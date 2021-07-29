# coding: utf-8
import logging
from copy import deepcopy
from typing import Any, Optional, Union, List

import numpy as np
import torch
import torch.optim as optim
import pytorch_lightning as pl
from pytorch_lightning import LightningModule, LightningDataModule
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor
from torch.optim.lr_scheduler import (
    StepLR,
    LambdaLR,
    CyclicLR,
    CosineAnnealingWarmRestarts,
    MultiStepLR,
)
from torch.utils.data import DataLoader

# from ..evaluator.metrics import metrics_dict

logger = logging.getLogger(__name__)


def get_optimizer(name: str, model, learning_rate, weight_decay):
    r"""Init the Optimizer

    Returns:
        torch.optim: the optimizer
    """
    OPTIMIZERS = {
        'adam': optim.Adam,
        'adamw': optim.AdamW,
        'sgd': optim.SGD,
        'adagrad': optim.Adagrad,
        'rmsprop': optim.RMSprop,
    }

    try:
        opt_cls = OPTIMIZERS[name.lower()]
        optimizer = opt_cls(
            model.parameters(), lr=learning_rate, weight_decay=weight_decay
        )
    except:
        logger.warning('Received unrecognized optimizer, set default Adam optimizer')
        optimizer = optim.Adam(
            model.parameters(), lr=learning_rate, weight_decay=weight_decay
        )
    return optimizer


def get_lr_scheduler(config, optimizer):
    orig_lr = config['learning_rate']
    lr_sch_config = deepcopy(config['lr_scheduler'])
    lr_sch_name = lr_sch_config.pop('name')

    if lr_sch_name == 'multi_step':
        return MultiStepLR(optimizer, milestones=[2, 6, 12], gamma=0.5)
    elif lr_sch_name == 'cos_anneal':
        return CosineAnnealingWarmRestarts(
            optimizer, T_0=4, T_mult=1, eta_min=orig_lr / 10.0
        )
    elif lr_sch_name == 'cyclic':
        return CyclicLR(
            optimizer,
            base_lr=orig_lr / 10.0,
            max_lr=orig_lr,
            step_size_up=2,
            cycle_momentum=False,
        )

    step_size = lr_sch_config['step_size']
    gamma = config['gamma']
    if step_size is None or gamma is None:
        return LambdaLR(optimizer, lr_lambda=lambda _: 1)
    return StepLR(optimizer, step_size, gamma=gamma)


class Accuracy(object):
    @classmethod
    def complete_match(cls, labels: List[List[str]], preds: List[List[str]]):
        assert len(labels) == len(preds)
        total_num = len(labels)
        hit_num = 0
        for label, pred in zip(labels, preds):
            if label == pred:
                hit_num += 1

        return hit_num / (total_num + 1e-6)

    @classmethod
    def label_match(cls, labels: List[List[str]], preds: List[List[str]]):
        assert len(labels) == len(preds)
        total_num = 0
        hit_num = 0
        for label, pred in zip(labels, preds):
            total_num += max(len(label), len(pred))
            min_len = min(len(label), len(pred))
            hit_num += sum([l == p for l, p in zip(label[:min_len], pred[:min_len])])

        return hit_num / (total_num + 1e-6)


class WrapperLightningModule(pl.LightningModule):
    def __init__(self, config, model):
        super().__init__()
        self.config = config
        self.model = model
        self._optimizer = get_optimizer(
            config['optimizer'],
            self.model,
            config['learning_rate'],
            config.get('weight_decay', 0),
        )

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx):
        if hasattr(self.model, 'set_current_epoch'):
            self.model.set_current_epoch(self.current_epoch)
        else:
            setattr(self.model, 'current_epoch', self.current_epoch)
        res = self.model.calculate_loss(batch)
        losses = res['loss']
        self.log(
            'train_loss',
            losses.item(),
            on_step=True,
            on_epoch=True,
            prog_bar=True,
            logger=True,
        )
        return losses

    def validation_step(self, batch, batch_idx):
        if hasattr(self.model, 'validation_step'):
            return self.model.validation_step(batch, batch_idx, self)

        res = self.model.calculate_loss(
            batch, return_model_output=True, return_preds=True
        )
        losses = res['loss']
        preds, _ = zip(*res['preds'])
        val_metrics = {'val_loss': losses.item()}

        labels_list = batch[2]
        val_metrics['complete_match'] = Accuracy.complete_match(labels_list, preds)
        val_metrics['label_match'] = Accuracy.label_match(labels_list, preds)

        # 过滤掉NaN的指标。有些指标在某些batch数据上会出现结果NaN，比如batch只有正样本或负样本时，AUC=NaN
        val_metrics = {k: v for k, v in val_metrics.items() if not np.isnan(v)}
        self.log_dict(
            val_metrics, on_step=True, on_epoch=True, prog_bar=True, logger=True,
        )
        return losses

    def configure_optimizers(self):
        return [self._optimizer], [get_lr_scheduler(self.config, self._optimizer)]


class PlTrainer(object):
    """
    封装 PyTorch Lightning 的训练器。
    """

    def __init__(self, config):
        self.config = config

        lr_monitor = LearningRateMonitor(logging_interval='step')
        callbacks = [lr_monitor]

        mode = self.config.get('pl_checkpoint_mode', 'min')
        monitor = self.config.get('pl_checkpoint_monitor')
        fn_fields = [self.__class__.__name__, '{epoch:03d}']
        if monitor:
            fn_fields.append('{' + monitor + ':.4f}')
            checkpoint_callback = ModelCheckpoint(
                monitor=monitor,
                mode=mode,
                filename='-'.join(fn_fields),
                save_last=True,
                save_top_k=5,
            )
            callbacks.append(checkpoint_callback)

        self.pl_trainer = pl.Trainer(
            # limit_train_batches=3,
            # limit_val_batches=2,
            gpus=self.config.get('gpus'),
            max_epochs=self.config.get('epochs', 20),
            precision=self.config.get('precision', 32),
            callbacks=callbacks,
            stochastic_weight_avg=True,
        )

    def fit(
        self,
        model: LightningModule,
        train_dataloader: Any = None,
        val_dataloaders: Optional[Union[DataLoader, List[DataLoader]]] = None,
        datamodule: Optional[LightningDataModule] = None,
    ):
        r"""
        Runs the full optimization routine.

        Args:
            model: Model to fit.

            train_dataloader: Either a single PyTorch DataLoader or a collection of these
                (list, dict, nested lists and dicts). In the case of multiple dataloaders, please
                see this :ref:`page <multiple-training-dataloaders>`

            val_dataloaders: Either a single Pytorch Dataloader or a list of them, specifying validation samples.
                If the model has a predefined val_dataloaders method this will be skipped

            datamodule: A instance of :class:`LightningDataModule`.

        """
        pl_module = WrapperLightningModule(self.config, model)
        res = self.pl_trainer.fit(
            pl_module, train_dataloader, val_dataloaders, datamodule
        )

        fields = self.pl_trainer.checkpoint_callback.best_model_path.rsplit(
            '.', maxsplit=1
        )
        fields[0] += '-model'
        output_model_fp = '.'.join(fields)
        resave_model(
            self.pl_trainer.checkpoint_callback.best_model_path, output_model_fp
        )
        self.saved_model_file = output_model_fp

        return res


def resave_model(module_fp, output_model_fp):
    """PlTrainer存储的文件对应其 `pl_module` 模块，需利用此函数转存为 `model` 对应的模型文件。"""
    checkpoint = torch.load(module_fp)
    state_dict = {}
    for k, v in checkpoint['state_dict'].items():
        state_dict[k.split('.', maxsplit=1)[1]] = v
    torch.save({'state_dict': state_dict}, output_model_fp)
