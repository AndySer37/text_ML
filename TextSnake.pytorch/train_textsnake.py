import os
import time
from datetime import datetime

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.utils.data as data
from torch.optim import lr_scheduler
from util.shedule import FixLR

from dataset.total_text import TotalText
from dataset.synth_text import SynthText
from network.loss import TextLoss
from network.textnet import TextNet
from util.augmentation import BaseTransform, Augmentation
from util.config import config as cfg, update_config, print_config
from util.misc import AverageMeter
from util.misc import mkdirs, to_device
from util.option import BaseOptions
from util.visualize import visualize_network_output
from util.summary import LogSummary

lr = None
train_step = 0

def save_model(model, epoch, lr, optimzer, _iter):

    save_dir = os.path.join(cfg.save_dir, cfg.exp_name)
    if not os.path.exists(save_dir):
        mkdirs(save_dir)

    save_path = os.path.join(save_dir, 'textsnake_{}_{}_{}.pth'.format(model.backbone_name, epoch, _iter))
    print('Saving to {}.'.format(save_path))
    state_dict = {
        'lr': lr,
        'epoch': epoch,
        'model': model.state_dict() if not cfg.mgpu else model.module.state_dict(),
        'optimizer': optimzer.state_dict()
    }
    torch.save(state_dict, save_path)


def load_model(model, model_path):
    print('Loading from {}'.format(model_path))
    state_dict = torch.load(model_path)
    model.load_state_dict(state_dict['model'])


def train(model, train_loader, criterion, scheduler, optimizer, epoch, logger):

    global train_step
    save_dir = os.path.join(cfg.save_dir, cfg.exp_name)
    losses = AverageMeter()
    batch_time = AverageMeter()
    data_time = AverageMeter()
    end = time.time()
    model.train()
    # scheduler.step()

    print('Epoch: {} : LR = {}'.format(epoch, lr))

    for i, (img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map, meta) in enumerate(train_loader):
        data_time.update(time.time() - end)

        train_step += 1

        img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map = to_device(
            img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map)

        output = model(img)
        tr_loss, tcl_loss, sin_loss, cos_loss, radii_loss = \
            criterion(output, tr_mask, tcl_mask, sin_map, cos_map, radius_map, train_mask)
        loss = tr_loss + tcl_loss + sin_loss + cos_loss + radii_loss

        # backward
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        losses.update(loss.item())
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if cfg.viz and i % cfg.viz_freq == 0:
            visualize_network_output(output, tr_mask, tcl_mask, mode='train')

        if i % cfg.display_freq == 0:
            print('({:d} / {:d}) - Loss: {:.4f} - tr_loss: {:.4f} - tcl_loss: {:.4f} - sin_loss: {:.4f} - cos_loss: {:.4f} - radii_loss: {:.4f}'.format(
                i, len(train_loader), loss.item(), tr_loss.item(), tcl_loss.item(), sin_loss.item(), cos_loss.item(), radii_loss.item())
            )
            with open("{0}/tr_loss_result.txt".format(save_dir),"a+") as f:
                f.write('{}_{:d}_{:.4f}\n'.format(epoch, i, tr_loss.item()))
            with open("{0}/tcl_loss_result.txt".format(save_dir),"a+") as f:
                f.write('{}_{:d}_{:.4f}\n'.format(epoch, i, tcl_loss.item()))
            with open("{0}/sin_result.txt".format(save_dir),"a+") as f:
                f.write('{}_{:d}_{:.4f}\n'.format(epoch, i, sin_loss.item()))
            with open("{0}/cos_result.txt".format(save_dir),"a+") as f:
                f.write('{}_{:d}_{:.4f}\n'.format(epoch, i, cos_loss.item()))
            with open("{0}/radii_result.txt".format(save_dir),"a+") as f:
                f.write('{}_{:d}_{:.4f}\n'.format(epoch, i, radii_loss.item()))
            with open("{0}/loss_result.txt".format(save_dir),"a+") as f:
                f.write('{}_{:d}_{:.4f}\n'.format(epoch, i, loss.item()))
        if i % cfg.viz_save_iter == 0:
            save_model(model, epoch, scheduler.get_lr(), optimizer, i)

        if i % cfg.log_freq == 0:
            logger.write_scalars({
                'loss': loss.item(),
                'tr_loss': tr_loss.item(),
                'tcl_loss': tcl_loss.item(),
                'sin_loss': sin_loss.item(),
                'cos_loss': cos_loss.item(),
                'radii_loss': radii_loss.item()
            }, tag='train', n_iter=train_step)

    if epoch % cfg.save_freq == 0:
        save_model(model, epoch, scheduler.get_lr(), optimizer, "end")

    print('Training Loss: {}'.format(losses.avg))
    scheduler.step()

def validation(model, valid_loader, criterion, epoch, logger):
    save_dir = os.path.join(cfg.save_dir, cfg.exp_name)
    with torch.no_grad():
        model.eval()
        losses = AverageMeter()
        tr_losses = AverageMeter()
        tcl_losses = AverageMeter()
        sin_losses = AverageMeter()
        cos_losses = AverageMeter()
        radii_losses = AverageMeter()

        for i, (img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map, meta) in enumerate(valid_loader):

            img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map = to_device(
                img, train_mask, tr_mask, tcl_mask, radius_map, sin_map, cos_map)

            output = model(img)

            tr_loss, tcl_loss, sin_loss, cos_loss, radii_loss = \
                criterion(output, tr_mask, tcl_mask, sin_map, cos_map, radius_map, train_mask)
            loss = tr_loss + tcl_loss + sin_loss + cos_loss + radii_loss

            # update losses
            losses.update(loss.item())
            tr_losses.update(tr_loss.item())
            tcl_losses.update(tcl_loss.item())
            sin_losses.update(sin_loss.item())
            cos_losses.update(cos_loss.item())
            radii_losses.update(radii_loss.item())

            if cfg.viz and i % cfg.viz_freq == 0:
                visualize_network_output(output, tr_mask, tcl_mask, mode='val')

            if i % cfg.display_freq == 0:
                print(
                    'Validation: - Loss: {:.4f} - tr_loss: {:.4f} - tcl_loss: {:.4f} - sin_loss: {:.4f} - cos_loss: {:.4f} - radii_loss: {:.4f}'.format(
                        loss.item(), tr_loss.item(), tcl_loss.item(), sin_loss.item(),
                        cos_loss.item(), radii_loss.item())
                )
                with open("{0}/tr_loss_result.txt".format(save_dir),"a+") as f:
                    f.write('{}_validation_{:.4f}\n'.format(epoch, tr_loss.item()))
                with open("{0}/tcl_loss_result.txt".format(save_dir),"a+") as f:
                    f.write('{}_validation_{:.4f}\n'.format(epoch, tcl_loss.item()))
                with open("{0}/sin_result.txt".format(save_dir),"a+") as f:
                    f.write('{}_validation_{:.4f}\n'.format(epoch, sin_loss.item()))
                with open("{0}/cos_result.txt".format(save_dir),"a+") as f:
                    f.write('{}_validation_{:.4f}\n'.format(epoch, cos_loss.item()))
                with open("{0}/radii_result.txt".format(save_dir),"a+") as f:
                    f.write('{}_validation_{:.4f}\n'.format(epoch, radii_loss.item()))
                with open("{0}/loss_result.txt".format(save_dir),"a+") as f:
                    f.write('{}_validation_{:.4f}\n'.format(epoch, loss.item()))



        logger.write_scalars({
            'loss': losses.avg,
            'tr_loss': tr_losses.avg,
            'tcl_loss': tcl_losses.avg,
            'sin_loss': sin_losses.avg,
            'cos_loss': cos_losses.avg,
            'radii_loss': radii_losses.avg
        }, tag='val', n_iter=epoch)

        print('Validation Loss: {}'.format(losses.avg))


def main():

    global lr

    if cfg.dataset == 'total-text':

        trainset = TotalText(
            data_root='data/total-text',
            ignore_list=None,
            is_training=True,
            transform=Augmentation(size=cfg.input_size, mean=cfg.means, std=cfg.stds)
        )

        valset = TotalText(
            data_root='data/total-text',
            ignore_list=None,
            is_training=False,
            transform=BaseTransform(size=cfg.input_size, mean=cfg.means, std=cfg.stds)
        )

    elif cfg.dataset == 'synth-text':
        trainset = SynthText(
            data_root='data/SynthText',
            is_training=True,
            transform=Augmentation(size=cfg.input_size, mean=cfg.means, std=cfg.stds)
        )
        valset = None
    else:
        pass

    train_loader = data.DataLoader(trainset, batch_size=cfg.batch_size, shuffle=True, num_workers=cfg.num_workers)
    if valset:
        val_loader = data.DataLoader(valset, batch_size=cfg.batch_size, shuffle=False, num_workers=cfg.num_workers)
    else:
        valset = None

    log_dir = os.path.join(cfg.log_dir, datetime.now().strftime('%b%d_%H-%M-%S_') + cfg.exp_name)
    logger = LogSummary(log_dir)

    # Model
    model = TextNet(is_training=True, backbone=cfg.net)
    if cfg.mgpu:
        model = nn.DataParallel(model)

    model = model.to(cfg.device)
    if cfg.cuda:
        cudnn.benchmark = True

    if cfg.resume:
        load_model(model, cfg.resume)

    criterion = TextLoss()
    lr = cfg.lr
    optimizer = torch.optim.Adam(model.parameters(), lr=cfg.lr)

    if cfg.dataset == 'synth-text':
        scheduler = FixLR(optimizer)
    else:
        scheduler = lr_scheduler.StepLR(optimizer, step_size=100, gamma=0.1)

    print('Start training TextSnake.')
    for epoch in range(cfg.start_epoch, cfg.max_epoch):
        train(model, train_loader, criterion, scheduler, optimizer, epoch, logger)
        if valset:
            validation(model, val_loader, criterion, epoch, logger)

    print('End.')

if __name__ == "__main__":
    # parse arguments
    option = BaseOptions()
    args = option.initialize()

    update_config(cfg, args)
    print_config(cfg)

    # main
    main()