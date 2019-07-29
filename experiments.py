import functools as ft
import itertools as it
from pathlib import Path

import click
import ignite
import torch
from ignite.engine import _prepare_batch as prepare_batch
from torch import nn
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms

from engine import (every_n, get_log_prefix, log_iterations_per_second,
                    step_lr_scheduler)
from optim import AdamW, LookaheadOptimizer
from tqdm import tqdm

try:
    from torch.utils.tensorboard import SummaryWriter
except (ModuleNotFoundError, ImportError):
    from tensorboardX import SummaryWriter

try:
    from apex import amp
    MIXED_PRECISION = True
except ModuleNotFoundError:
    print('Could not find APEX for mixed precision training')
    MIXED_PRECISION = False


def build_data(num_workers=2):
    data = {
        'train': datasets.CIFAR10(
            root='./__pycache__', download=True,
            transform=transforms.Compose([
                transforms.RandomCrop((32, 32), padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor()]
            )
        ),
        'valid': datasets.CIFAR10(
            root='./__pycache__', train=False, download=True,
            transform=transforms.Compose([transforms.ToTensor()])
        )
    }

    loaders = {
        'train': DataLoader(data['train'], batch_size=128, shuffle=True,
                            num_workers=num_workers, pin_memory=True,
                            drop_last=True),
        'valid': DataLoader(data['valid'], batch_size=128,
                            num_workers=num_workers, pin_memory=True,
                            drop_last=False)
    }

    return data, loaders


def create_supervised_trainer(model, optimizer, loss_fn,
                              device=None, non_blocking=False):
    if device:
        model.to(device)

    def _update(engine, batch):
        model.train()
        optimizer.zero_grad()
        x, y = prepare_batch(batch, device=device, non_blocking=non_blocking)
        y_pred = model(x)
        loss = loss_fn(y_pred, y)

        if MIXED_PRECISION:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        optimizer.step()
        return loss.item()

    return ignite.engine.Engine(_update)


def train_cifar10(workdir, optimizer_callback, epochs=200, apex_opt_level=None):
    if apex_opt_level is None:
        global MIXED_PRECISION
        MIXED_PRECISION = False

    workdir = Path(workdir)
    workdir.mkdir(exist_ok=True, parents=True)

    device = 'cuda:0' if torch.cuda.device_count() > 0 else 'cpu'

    _, loaders = build_data(num_workers=4)

    model = models.resnet18(pretrained=False, num_classes=10)
    model.avgpool = nn.Sequential()

    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)
        print('Creating DataParallel model')
        device = None
    else:
        model = model.to(device)

    optimizer = optimizer_callback(model.parameters())
    if MIXED_PRECISION:
        model, optimizer = amp.initialize(model, optimizer, opt_level=apex_opt_level)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=[60, 120, 160], gamma=0.2)
    trainer = create_supervised_trainer(
        model, optimizer, nn.CrossEntropyLoss(), device=device,
        non_blocking=True)

    metrics = {
        'loss': ignite.metrics.Loss(nn.functional.cross_entropy),
        'accuracy': ignite.metrics.Accuracy()}
    evaluator = ignite.engine.create_supervised_evaluator(
        model, metrics=metrics, device=device, non_blocking=True)
    writer = SummaryWriter(workdir)
    status = tqdm(total=len(loaders['train']) * loaders['train'].batch_size * epochs)

    @trainer.on(ignite.engine.Events.ITERATION_COMPLETED)
    @every_n(n=50)
    def log_training_progress(engine):
        prefix = get_log_prefix(engine)
        status.set_description(f'{prefix} loss={engine.state.output:.04f}')
        status.update(engine.state.iteration)

    @trainer.on(ignite.engine.Events.EPOCH_COMPLETED)
    def run_evaluator(engine):  # pylint: disable=unused-variable
        model.eval()
        for split, loader in loaders.items():
            evaluator.run(loader)

            for name, value in evaluator.state.metrics.items():  # pylint: disable=no-member
                writer.add_scalar(f'metrics/{split}_{name}', value, engine.state.epoch)

    trainer.add_event_handler(
        ignite.engine.Events.EPOCH_STARTED,
        step_lr_scheduler(optimizer, scheduler, summary_writer=writer, verbose=False))
    trainer.add_event_handler(
        ignite.engine.Events.ITERATION_COMPLETED,
        log_iterations_per_second(summary_writer=writer, verbose=False))

    trainer.run(loaders['train'], max_epochs=epochs)


@click.command('cifar10')
@click.option('--workdir', '-w', type=click.Path(file_okay=False, writable=True),
              required=True)
@click.option('--optimizer', '-o', required=True)
@click.option('--apex-opt-level', type=click.Choice(['O0', 'O1', 'O2', 'O3']))
def cifar10(workdir, optimizer, apex_opt_level):
    optimizer_callbacks = {
        'sgd': lambda p: torch.optim.SGD(p, lr=0.02, momentum=0.9, weight_decay=0.0001),
        'adamw': lambda p: AdamW(p, lr=1e-3, weight_decay=0.3),
        'adam': lambda p: torch.optim.Adam(p, lr=1e-3, weight_decay=0.3),
        'lookahead': lambda p: LookaheadOptimizer(
            p, slow_update_rate=0.5, lookahead_steps=5, lr=0.1,
            momentum=0.9, weight_decay=0.0001)
    }
    train_cifar10(workdir, optimizer_callbacks[optimizer],
                  apex_opt_level=apex_opt_level)


if __name__ == '__main__':
    cifar10()  # pylint: disable=no-value-for-parameter
