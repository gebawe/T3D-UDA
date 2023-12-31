# -*- coding:utf-8 -*-
# author: Awet H. Gebrehiwot
# at 8/10/22
# --------------------------|
import argparse
import os
import sys
import time
import warnings

import numpy as np
import torch
import torch.optim as optim
from torch.nn.parallel import DistributedDataParallel
from tqdm import tqdm

from builder import data_builder, model_builder, loss_builder
from configs.config import load_config_data
from dataloader.pc_dataset import get_label_name, update_config
from utils.load_save_util import load_checkpoint
from utils.metric_util import per_class_iu, fast_hist_crop
from utils.per_class_weight import semantic_kitti_class_weights
import copy

def yield_target_dataset_loader(n_epochs, target_train_dataset_loader):
    for e in range(n_epochs):
        for i_iter_train, (_, train_vox_label, train_grid, _, train_pt_fea, ref_st_idx, ref_end_idx, lcw) \
                in enumerate(target_train_dataset_loader):
            yield train_vox_label, train_grid, train_pt_fea, ref_st_idx, ref_end_idx, lcw

class Trainer(object):
    def __init__(self, student_model,
                 teacher_model,
                 optimizer_teacher,
                 optimizer_student,
                 teacher_ckpt_dir,
                 student_ckpt_dir,
                 unique_label,
                 unique_label_str,
                 lovasz_softmax_teacher,
                 loss_func_teacher,
                 lovasz_softmax_student,
                 loss_func_student,
                 ignore_label,
                 train_mode=None,
                 ssl=None,
                 eval_frequency=1,
                 pytorch_device=0,
                 warmup_epoch=1,
                 ema_frequency=5):
        self.student_model = student_model
        self.teacher_model = teacher_model
        self.optimizer_teacher = optimizer_teacher
        self.optimizer_student = optimizer_student
        self.teacher_model_save_path = teacher_ckpt_dir
        self.student_model_save_path = student_ckpt_dir
        self.unique_label = unique_label
        self.unique_label_str = unique_label_str
        self.eval_frequency = eval_frequency
        self.lovasz_softmax_teacher = lovasz_softmax_teacher
        self.loss_func_teacher = loss_func_teacher
        self.lovasz_softmax_student = lovasz_softmax_student
        self.loss_func_student = loss_func_student
        self.ignore_label = ignore_label
        self.train_mode = train_mode
        self.ssl = ssl
        self.pytorch_device = pytorch_device
        self.warmup_epoch = warmup_epoch
        self.ema_frequency = ema_frequency
        self.val_teacher = False
        self.val_student = False
        self.teacher_best_val_miou = 0
        self.student_best_val_miou = 0
        self.ema_update_now = False

    def criterion(self, outputs, point_label_tensor, lcw=None, mode='Teacher'):
        if self.ssl:
            if mode == 'Teacher':
                lcw_tensor = torch.FloatTensor(lcw).to(self.pytorch_device)
            else:
                lcw_tensor = lcw

            loss = self.lovasz_softmax_student(torch.nn.functional.softmax(outputs), point_label_tensor,
                                       ignore=self.ignore_label, lcw=lcw_tensor) \
                   + self.loss_func_student(outputs, point_label_tensor, lcw=lcw_tensor)
        elif mode == 'Student':
            lcw_tensor = lcw
            loss = self.lovasz_softmax_student(torch.nn.functional.softmax(outputs), point_label_tensor,
                                       ignore=self.ignore_label, lcw=lcw_tensor) \
                   + self.loss_func_student(outputs, point_label_tensor, lcw=lcw_tensor)
        else:
            loss = self.lovasz_softmax_teacher(torch.nn.functional.softmax(outputs), point_label_tensor,
                                       ignore=self.ignore_label) \
                   + self.loss_func_teacher(outputs, point_label_tensor)
        return loss

    # initialize student model from teacher model weights
    @torch.no_grad()
    def _initialize_student_model_from_teacher(self):
        teacher_model_dict = self.teacher_model.state_dict()
        self.student_model.load_state_dict(teacher_model_dict)

    # updating teacher model weights
    @torch.no_grad()
    def _update_teacher_model(self, keep_rate=0.996):
        student_model_dict = self.student_model.state_dict()

        new_teacher_dict = copy.copy(self.teacher_model.state_dict())  # OrderedDict()
        # print(f"before: {self.teacher_model.state_dict()}")
        for key, value in self.teacher_model.state_dict().items():
            if key in student_model_dict.keys():
                new_teacher_dict[key] = (
                        student_model_dict[key] *
                        (1 - keep_rate) + value * keep_rate
                )
            # else:
            #    print("{} is not found in student model".format(key))
            #    #raise Exception("{} is not found in student model".format(key))
        # print(f"new_teacher_dict: {new_teacher_dict}")
        self.teacher_model.load_state_dict(new_teacher_dict)
        # print(f"after: {self.teacher_model.state_dict()}")
    def validate(self, my_model, val_dataset_loader, val_batch_size, test_loader=None, ssl=None):
        hist_list = []
        val_loss_list = []
        my_model.eval()
        with torch.no_grad():
            for i_iter_val, (
                    _, val_vox_label, val_grid, val_pt_labs, val_pt_fea, ref_st_idx, ref_end_idx, lcw) in enumerate(
                val_dataset_loader):
                val_pt_fea_ten = [torch.from_numpy(i).type(torch.FloatTensor).to(self.pytorch_device) for i in val_pt_fea]
                val_grid_ten = [torch.from_numpy(i).to(self.pytorch_device) for i in val_grid]
                val_label_tensor = val_vox_label.type(torch.LongTensor).to(self.pytorch_device)

                predict_labels = my_model(val_pt_fea_ten, val_grid_ten, val_batch_size)
                # aux_loss = loss_fun(aux_outputs, point_label_tensor)

                inp = val_label_tensor.size(0)

                # TODO: check if this is correctly implemented
                # hack for batch_size mismatch with the number of training example
                predict_labels = predict_labels[:inp, :, :, :, :]

                loss = self.criterion(predict_labels, val_label_tensor, lcw)

                predict_labels = torch.argmax(predict_labels, dim=1)
                predict_labels = predict_labels.cpu().detach().numpy()
                for count, i_val_grid in enumerate(val_grid):
                    hist_list.append(fast_hist_crop(predict_labels[
                                                        count, val_grid[count][:, 0], val_grid[count][:, 1],
                                                        val_grid[count][:, 2]], val_pt_labs[count],
                                                    self.unique_label))
                val_loss_list.append(loss.detach().cpu().numpy())

        return hist_list, val_loss_list


    def fit(self, n_epochs, source_train_dataset_loader, train_batch_size, val_dataset_loader,
            val_batch_size, test_loader=None,
            ckpt_save_interval=5, lr_scheduler_each_iter=False):

        global_iter = 0
        best_val_miou = 0

        for epoch in range(n_epochs):

            pbar = tqdm(total=len(source_train_dataset_loader))
            # train the model
            loss_list = []
            self.teacher_model.train()
            # training with multi-frames and ssl:
            for i_iter_train, (
                    _, train_vox_label, train_grid, _, train_pt_fea, ref_st_idx, ref_end_idx, lcw) in enumerate(
                source_train_dataset_loader):
                # call the validation and inference with
                train_pt_fea_ten = [torch.from_numpy(i).type(torch.FloatTensor).to(self.pytorch_device) for i in
                                    train_pt_fea]
                # train_grid_ten = [torch.from_numpy(i[:,:2]).to(self.pytorch_device) for i in train_grid]
                train_vox_ten = [torch.from_numpy(i).to(self.pytorch_device) for i in train_grid]
                point_label_tensor = train_vox_label.type(torch.LongTensor).to(self.pytorch_device)

                # forward + backward + optimize
                outputs = self.teacher_model(train_pt_fea_ten, train_vox_ten, train_batch_size)
                inp = point_label_tensor.size(0)
                # print(f"outputs.size() : {outputs.size()}")
                # TODO: check if this is correctly implemented
                # hack for batch_size mismatch with the number of training example
                outputs = outputs[:inp, :, :, :, :]
                ################################

                loss = self.criterion(outputs, point_label_tensor, lcw)

                # TODO: check --> to mitigate only one element tensors can be converted to Python scalars
                # loss = loss.mean()
                loss.backward()
                self.optimizer_teacher.step()
                self.optimizer_teacher.zero_grad()

                # Uncomment to use the learning rate scheduler
                # scheduler.step()

                loss_list.append(loss.item())

                if global_iter % 500 == 0:
                    pbar.update(500)
                    if len(loss_list) > 0:
                        print('epoch %d iter %5d, loss: %.3f\n' % (epoch, i_iter_train, np.mean(loss_list)))
                    else:
                        print('loss error')
                global_iter += 1

            # ----------------------------------------------------------------------#
            # Evaluation/validation
            with torch.no_grad():
                hist_list, val_loss_list = self.validate(self.teacher_model, val_dataset_loader, val_batch_size, test_loader, self.ssl)

            # ----------------------------------------------------------------------#
            # Print validation mIoU and Loss
            print(f"--------------- epoch: {epoch} ----------------")
            iou = per_class_iu(sum(hist_list))
            print('Validation per class iou: ')
            for class_name, class_iou in zip(self.unique_label_str, iou):
                print('%s : %.2f%%' % (class_name, class_iou * 100))
            val_miou = np.nanmean(iou) * 100
            # del val_vox_label, val_grid, val_pt_fea

            # save model if performance is improved
            if best_val_miou < val_miou:
                best_val_miou = val_miou
                torch.save(self.teacher_model.state_dict(), self.teacher_model_save_path)

            print('Current val miou is %.3f while the best val miou is %.3f' %
                  (val_miou, best_val_miou))
            # print('Current val loss is %.3f' % (np.mean(val_loss_list)))

    def validate_uda(self, val_dataset_loader, val_batch_size, test_loader=None, ssl=None):
        teacher_hist_list = []
        teacher_val_loss_list = []
        student_hist_list = []
        student_val_loss_list = []
        self.teacher_model.eval()
        self.student_model.eval()
        with torch.no_grad():
            for i_iter_val, (_, val_vox_label, val_grid, val_pt_labs, val_pt_fea, ref_st_idx, ref_end_idx, lcw) in enumerate(
                val_dataset_loader):
                val_pt_fea_ten = [torch.from_numpy(i).type(torch.FloatTensor).to(self.pytorch_device) for i in val_pt_fea]
                val_grid_ten = [torch.from_numpy(i).to(self.pytorch_device) for i in val_grid]
                val_label_tensor = val_vox_label.type(torch.LongTensor).to(self.pytorch_device)
                inp = val_label_tensor.size(0)

                if self.val_teacher:
                    predict_labels = self.teacher_model(val_pt_fea_ten, val_grid_ten, val_batch_size)
                    # aux_loss = loss_fun(aux_outputs, point_label_tensor)
                    # TODO: check if this is correctly implemented
                    # hack for batch_size mismatch with the number of training example
                    predict_labels = predict_labels[:inp, :, :, :, :]
                    loss = self.criterion(predict_labels, val_label_tensor, lcw)
                    predict_labels = torch.argmax(predict_labels, dim=1)
                    predict_labels = predict_labels.cpu().detach().numpy()
                    for count, i_val_grid in enumerate(val_grid):
                        teacher_hist_list.append(fast_hist_crop(predict_labels[
                                                            count, val_grid[count][:, 0], val_grid[count][:, 1],
                                                            val_grid[count][:, 2]], val_pt_labs[count],
                                                        self.unique_label))
                    # teacher_val_loss_list.append(loss.detach().cpu().numpy())

                if self.val_student:
                    predict_labels = self.student_model(val_pt_fea_ten, val_grid_ten, val_batch_size)
                    # aux_loss = loss_fun(aux_outputs, point_label_tensor)
                    # TODO: check if this is correctly implemented
                    # hack for batch_size mismatch with the number of training example
                    predict_labels = predict_labels[:inp, :, :, :, :]
                    loss = self.criterion(predict_labels, val_label_tensor, lcw)
                    predict_labels = torch.argmax(predict_labels, dim=1)
                    predict_labels = predict_labels.cpu().detach().numpy()
                    for count, i_val_grid in enumerate(val_grid):
                        student_hist_list.append(fast_hist_crop(predict_labels[
                                                            count, val_grid[count][:, 0], val_grid[count][:, 1],
                                                            val_grid[count][:, 2]], val_pt_labs[count],
                                                        self.unique_label))
                    # student_val_loss_list.append(loss.detach().cpu().numpy())

        return teacher_hist_list, student_hist_list

    def forward(self, model, train_vox_label, train_grid, train_pt_fea, train_batch_size, mode='Train'):
        grad = False
        if mode == 'Train':
            model.train()
            grad = True
        elif mode == 'Pseudo_Labeling':
            model.eval()
            grad = False
        with torch.set_grad_enabled(grad):
            train_pt_fea_ten = [torch.from_numpy(i).type(torch.FloatTensor).to(self.pytorch_device) for i in
                                train_pt_fea]
            train_vox_ten = [torch.from_numpy(i).to(self.pytorch_device) for i in train_grid]
            point_label_tensor = train_vox_label.type(torch.LongTensor).to(self.pytorch_device)

            # forward + backward + optimize
            outputs = model(train_pt_fea_ten, train_vox_ten, train_batch_size)
            inp = point_label_tensor.size(0)
            # print(f"outputs.size() : {outputs.size()}")
            # TODO: check if this is correctly implemented
            # hack for batch_size mismatch with the number of training example
            outputs = outputs[:inp, :, :, :, :]

            return outputs, point_label_tensor


    def uda_fit(self, n_epochs, source_train_dataset_loader, source_train_batch_size,
                target_train_dataset_loader, target_train_batch_size, val_dataset_loader,
                val_batch_size, test_loader=None, ckpt_save_interval=5, lr_scheduler_each_iter=False):

        # call the target data generator function
        target_data_generator = yield_target_dataset_loader(n_epochs, target_train_dataset_loader)
        best_val_miou = 0
        global_iter = 1
        for epoch in range(n_epochs):
            pbar = tqdm(total=len(source_train_dataset_loader))
            # train the model
            loss_list = []
            # switch the teacher model validation to False
            self.val_teacher = False
            # switch the student model validation to False
            self.val_student = False
            # swith teacher weight update using ema to False
            self.ema_update_now = False
            # training with multi-frames and ssl:
            for i_iter_train, (
                    _, source_train_vox_label, source_train_grid, _, source_train_pt_fea, source_ref_st_idx,
                    source_ref_end_idx, source_lcw) in enumerate(
                source_train_dataset_loader):

                #####################################################

                if (epoch == self.warmup_epoch) and (i_iter_train == 0):
                    # create student model
                    # self.model = list(self.model)  # where attribute was dict_keys
                    # self.student_model = copy.deepcopy(self.teacher_model)
                    # initialize a student model form teacher model weights
                    self._initialize_student_model_from_teacher()

                # train teacher model in the burn in stage
                if epoch < self.warmup_epoch:
                    self.teacher_model.train()
                    source_output, source_point_label_tensor = self.forward(self.teacher_model, source_train_vox_label,
                                                                            source_train_grid, source_train_pt_fea,
                                                                            source_train_batch_size, mode='Train')
                    loss = self.criterion(source_output, source_point_label_tensor, source_lcw)

                    # TODO: check --> to mitigate only one element tensors can be converted to Python scalars
                    # loss = loss.mean()
                    # print(source_output.size(), source_point_label_tensor.size(), loss)
                    loss.backward()
                    self.optimizer_teacher.step()
                    self.optimizer_teacher.zero_grad()
                    # Uncomment to use the learning rate scheduler
                    # scheduler.step()
                    loss_list.append(loss.item())
                    # switch the teacher model validation to True
                    self.val_teacher = True

                ################################
                # T-UDA: Student - Teacher mutual learning
                else:
                    # ----------------------------------------------------#
                    # Student <-------> Teacher mutiual learning block
                    # Change teacher model to evaluation mode
                    self.teacher_model.eval()
                    # change student model to training mode
                    self.student_model.train()
                    # Student forward pass on Source data
                    source_output, source_point_label_tensor = self.forward(self.student_model, source_train_vox_label,
                                                                            source_train_grid, source_train_pt_fea,
                                                                            source_train_batch_size, mode='Train')

                    # Target dataset generator
                    target_train_vox_label, target_train_grid, target_train_pt_fea, target_ref_st_idx, target_ref_end_idx, target_lcw = next(
                        target_data_generator)

                    # Student forward pass on Target data
                    target_output, target_point_label_tensor = self.forward(self.student_model,
                                                                            target_train_vox_label,
                                                                            target_train_grid,
                                                                            target_train_pt_fea,
                                                                            target_train_batch_size,
                                                                            mode='Train')

                    # --- Pseudo Labeling--------#
                    # Teacher inference/forward pass on target data for Pseudo Labeling
                    self.teacher_model.eval()
                    target_prediction, target_point_label_tensor = self.forward(self.teacher_model,
                                                                                target_train_vox_label,
                                                                                target_train_grid,
                                                                                target_train_pt_fea,
                                                                                target_train_batch_size,
                                                                                mode='Pseudo_Labeling')
                    # pseudo label generated by teacher model
                    pseudo_label = torch.argmax(target_prediction, dim=1)

                    predict_probability = torch.nn.functional.softmax(target_prediction, dim=1)
                    # pseudo label confidence ---> lcw
                    pseudo_labels_prob_lcw, predict_prob_ind = predict_probability.max(dim=1)
                    # multiply by 100
                    pseudo_labels_prob_lcw = pseudo_labels_prob_lcw * 100

                    # Create a lcw tensor of ones for the source data and multiply by 100
                    source_lcw = torch.ones_like(source_point_label_tensor) * 100
                    ###
                    # loss calculation
                    loss = self.criterion(source_output, source_point_label_tensor, source_lcw, 'Student') \
                           + self.criterion(target_output, pseudo_label, pseudo_labels_prob_lcw, 'Student')

                    # TODO: check --> to mitigate only one element tensors can be converted to Python scalars
                    # loss = loss.mean()
                    # print(loss)
                    loss.backward()
                    self.optimizer_student.step()
                    self.optimizer_student.zero_grad()
                    # Uncomment to use the learning rate scheduler
                    # scheduler.step()
                    loss_list.append(loss.item())
                    # switch the student model validation to True
                    self.val_student = True
                    self.val_teacher = True

                if global_iter % 500 == 0:
                    pbar.update(500)
                    if len(loss_list) > 0:
                        print('epoch %d iter %5d, loss: %.3f\n' % (epoch, i_iter_train, np.mean(loss_list)))
                    else:
                        print('loss error')
                global_iter += 1

            # ----------------------------------------------------------------------#
            # Evaluation/validation
            with torch.no_grad():
                # Change teacher & student model to evaluation mode
                self.teacher_model.eval()
                self.student_model.eval()
                teacher_hist_list, student_hist_list = self.validate_uda(val_dataset_loader, val_batch_size, test_loader, self.ssl)

            # ----------------------------------------------------------------------#
            # Print validation mIoU and Loss
            print(f"--------------- epoch: {epoch} ----------------")
            # teacher validation
            if self.val_teacher:
                iou = per_class_iu(sum(teacher_hist_list))
                print('Teacher Validation per class iou: ')
                for class_name, class_iou in zip(self.unique_label_str, iou):
                    print('%s : %.2f%%' % (class_name, class_iou * 100))
                teacher_val_miou = np.nanmean(iou) * 100
                # save teacher model if performance is improved
                if self.teacher_best_val_miou < teacher_val_miou:
                    self.teacher_best_val_miou = teacher_val_miou
                    torch.save(self.teacher_model.state_dict(), self.teacher_model_save_path)

                print('Current teacher val miou is %.3f while the best  val miou is %.3f' % (teacher_val_miou, self.teacher_best_val_miou))
                # del val_vox_label, val_grid, val_pt_fea

            # teacher validation
            if self.val_student:
                iou = per_class_iu(sum(student_hist_list))
                print('Student Validation per class iou: ')
                for class_name, class_iou in zip(self.unique_label_str, iou):
                    print('%s : %.2f%%' % (class_name, class_iou * 100))
                student_val_miou = np.nanmean(iou) * 100
                # save student model if performance is improved
                if self.student_best_val_miou < student_val_miou:
                    self.student_best_val_miou = student_val_miou
                    self.ema_update_now = True
                print('Current Student val miou is %.3f while the best val miou is %.3f' % (student_val_miou, self.student_best_val_miou))

                if self.ema_update_now:
                    # -------EMA ----------------#
                    # EMA: Student ---> Teacher
                    self._update_teacher_model()
                    print("--------------- EMA - Update Performed ----------------")
                    torch.save(self.teacher_model.state_dict(), self.teacher_model_save_path)
                    # switch the teacher model validation to True
                    self.val_teacher = True
                    # save student model
                    torch.save(self.student_model.state_dict(), self.student_model_save_path)

            # # -------EMA ----------------#
            # # EMA: Student ---> Teacher
            # if ((epoch - self.warmup_epoch) % self.ema_frequency == 0) and (epoch > self.warmup_epoch):
            #     if self.student_best_val_miou < student_val_miou:
            #         self._update_teacher_model()
            #         print("--------------- EMA - Update Performed ----------------")
            #         torch.save(self.teacher_model.state_dict(), self.teacher_model_save_path)
            #         # switch the teacher model validation to True
            #     self.val_teacher = True


