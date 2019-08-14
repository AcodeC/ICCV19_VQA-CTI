"""
This code is modified from jnhwkim's repository.
https://github.com/jnhwkim/ban-vqa
"""
import os
import time
import torch
import utils
import torch.nn as nn
from trainer_OE import Trainer
from trainer_fp16 import FP16Trainer
import loss_function
warmup_updates = 4000


def init_weights(m):
    if type(m) == nn.Linear:
        with torch.no_grad():
            torch.nn.init.kaiming_normal_(m.weight)
            # m.bias.data.fill_(0.01)


def compute_score_with_logits(logits, labels):
    logits = torch.max(logits, 1)[1].data  # argmax
    one_hots = torch.zeros(*labels.size()).to(logits.device)
    one_hots.scatter_(1, logits.view(-1, 1), 1)
    scores = (one_hots * labels)
    return scores


def create_onehot_centroid(num_centroid):
    centroids = [indice for indice in range(num_centroid)]
    centroids = torch.Tensor(centroids).unsqueeze(1).long()
    onehot_centroid = torch.zeros(num_centroid,num_centroid)
    onehot_centroid.scatter_(1,centroids,1)
    return  onehot_centroid


def train(args, model, train_loader, eval_loader, num_epochs, output, opt=None, s_epoch=0):
    device = args.device
    # lr_default = 1e-3 if eval_loader is not None else args.lr
    lr_default = args.lr
    lr_decay_step = 2
    lr_decay_rate = .25
    lr_decay_epochs = range(10, 20, lr_decay_step) if eval_loader is not None else range(10, 20, lr_decay_step)
    gradual_warmup_steps = [0.5 * lr_default, 1.0 * lr_default, 1.5 * lr_default, 2.0 * lr_default]
    saving_epoch = 9
    grad_clip = args.clip_norm
    utils.create_dir(output)
    optim = torch.optim.Adamax(filter(lambda p: p.requires_grad, model.parameters()), lr=lr_default) \
        if opt is None else opt
    if args.distillation:
        criterion = loss_function.Distillation_Loss(T=args.T, alpha=args.alpha)

    else:
        criterion = torch.nn.BCEWithLogitsLoss(reduction='sum')

    logger = utils.Logger(os.path.join(output, 'log.txt'))
    logger.write(args.__repr__())
    best_eval_score = 0

    if args.weight_init == "kaiming_normal":
        model.apply(init_weights)

    utils.print_model(model, logger)
    logger.write('optim: adamax lr=%.4f, decay_step=%d, decay_rate=%.2f, grad_clip=%.2f' % \
        (lr_default, lr_decay_step, lr_decay_rate, grad_clip))
    # Create trainer
    if args.fp16:  # Using FP-16 for training
        trainer = FP16Trainer(args, model, criterion, lr_default, optim)
    else:  # Using FP-32 for training
        trainer = Trainer(args, model, criterion, optim)
    update_freq = int(args.update_freq)
    wall_time_start = time.time()
    for epoch in range(s_epoch, num_epochs):
        total_loss = 0
        train_score = 0
        total_norm = 0
        count_norm = 0
        num_updates = 0
        t = time.time()
        N = len(train_loader.dataset)
        num_batches = int(N/args.batch_size + 1)
        if epoch < len(gradual_warmup_steps):
            trainer.optimizer.param_groups[0]['lr'] = gradual_warmup_steps[epoch]
            logger.write('gradual warmup lr: %.8f' % trainer.optimizer.param_groups[0]['lr'])
        elif epoch in lr_decay_epochs:
            trainer.optimizer.param_groups[0]['lr'] *= lr_decay_rate
            logger.write('decreased lr: %.8f' % trainer.optimizer.param_groups[0]['lr'])
        else:
            logger.write('lr: %.8f' % trainer.optimizer.param_groups[0]['lr'])
        for i, (v, b, q, a, t_logits) in enumerate(train_loader):
            if args.fp16:
                v = v.to(device).half()
                b = b.to(device).half()
                q = q.to(device)
                a = a.to(device)
            else:
                v = v.to(device)
                b = b.to(device)
                q = q.to(device)
                a = a.to(device)
                t_logits = t_logits.to(device)

            sample = [v, b, q, a, t_logits]
            if i < num_batches - 1 and (i + 1) % update_freq > 0:
                trainer.train_step(sample, update_params=False)
            else:

                loss, grad_norm, batch_score = trainer.train_step(sample, update_params=True)
                total_norm += grad_norm
                count_norm += 1
                total_loss += loss.item()
                train_score += batch_score
                num_updates += 1
                if num_updates % int(args.print_interval / update_freq) == 0:
                    if args.fp16:
                        print("Iter: {}, Loss {:.4f}, Norm: {:.4f}, Total norm: {:.4f}, Num updates: {}, Loss scale: {}, "
                              "Wall time: {:.2f}, ETA: {}".format(i + 1, total_loss / ((num_updates + 1)),
                              grad_norm, total_norm, num_updates, trainer.get_loss_scale(),
                              time.time() - wall_time_start, utils.time_since(t, i / num_batches)))
                    else:
                        print("Iter: {}, Loss {:.4f}, VQA Loss {:.4f}, Norm: {:.4f}, Total norm: {:.4f},"
                              " Num updates: {}, Wall time: {:.2f}, ETA: {}".format(i + 1, total_loss / ((num_updates + 1)),
                              total_loss / ((num_updates + 1)), grad_norm, total_norm, num_updates,
                              time.time() - wall_time_start, utils.time_since(t, i / num_batches)))
                    if args.testing:
                        break

        total_loss /= num_updates
        train_score = 100 * train_score / (num_updates * args.batch_size)

        if eval_loader is not None:
            print("Evaluating...")
            trainer.model.train(False)
            # torch.cuda.empty_cache()
            eval_score, bound = evaluate(model, eval_loader, args)
            trainer.model.train(True)

        logger.write('epoch %d, time: %.2f' % (epoch, time.time()-t))
        logger.write('\ttrain_loss: %.2f, norm: %.4f, score: %.2f' % (total_loss, total_norm/count_norm, train_score))
        if eval_loader is not None:
            logger.write('\teval score: %.2f (%.2f)' % (100 * eval_score, 100 * bound))

        # Save per epoch
        if epoch >= saving_epoch:
            model_path = os.path.join(output, 'model_epoch%d.pth' % epoch)
            utils.save_model(model_path, model, epoch, trainer.optimizer)
            # Save best epoch
            if eval_loader is not None and eval_score > best_eval_score:
                model_path = os.path.join(output, 'model_epoch_best.pth')
                utils.save_model(model_path, model, epoch, trainer.optimizer)
                best_eval_score = eval_score


def evaluate(model, dataloader, args):
    device = args.device
    score = 0
    question_type_score = 0
    upper_bound = 0
    question_type_upper_bound = 0
    num_data = 0
    with torch.no_grad():
        for v, b, q, a, _ in iter(dataloader):
            v = v.to(device)
            b = b.to(device)
            q = q.to(device)
            a = a.to(device)
            final_preds = None

            if args.model == "ban":
                final_preds, _ = model(v, b, q, a)

            if args.model == "pdban":
                final_preds, _ = model(v, b, q, a)

            if args.model == "tan":
                final_preds = model(v, q, a)

            if args.model == "stacked_attention":
                final_preds = model(v, q)

            batch_score = compute_score_with_logits(final_preds, a.to(device)).sum()
            score += batch_score
            upper_bound += (a.max(1)[0]).sum()
            num_data += final_preds.size(0)

    score = score / len(dataloader.dataset)
    upper_bound = upper_bound / len(dataloader.dataset)
    if "qt" in args.model:
        question_type_score /= len(dataloader.dataset)
        question_type_upper_bound /= len(dataloader.dataset)

    if "qt" in args.model:
        return score, upper_bound, question_type_score, question_type_upper_bound
    return score, upper_bound