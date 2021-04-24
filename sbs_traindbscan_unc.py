from __future__ import print_function, absolute_import
import argparse
import os.path as osp
import random
import numpy as np
import sys


from sklearn.cluster import DBSCAN
# from sklearn.preprocessing import normalize

import torch
from torch import nn
from torch.backends import cudnn
from torch.utils.data import DataLoader
import torch.nn.functional as F
# from torch.nn import init
#
# from UDAsbs.utils.rerank import compute_jaccard_dist

from UDAsbs import datasets, sinkhornknopp as sk
from UDAsbs import models
from UDAsbs.trainers import DbscanBaseTrainer_unc_ema
from UDAsbs.evaluators import Evaluator, extract_features
from UDAsbs.utils.data import IterLoader
from UDAsbs.utils.data import transforms as T
from UDAsbs.utils.data.sampler import RandomMultipleGallerySampler
from UDAsbs.utils.data.preprocessor import Preprocessor
from UDAsbs.utils.logging import Logger
from UDAsbs.utils.serialization import load_checkpoint, save_checkpoint#, copy_state_dict

from UDAsbs.memorybank.NCEAverage import onlinememory
from UDAsbs.utils.faiss_rerank import compute_jaccard_distance
# import ipdb


start_epoch = best_mAP = 0

def get_data(name, data_dir, l=1):
    root = osp.join(data_dir)

    dataset = datasets.create(name, root, l)

    label_dict = {}
    for i, item_l in enumerate(dataset.train):
        # dataset.train[i]=(item_l[0],0,item_l[2])
        if item_l[1] in label_dict:
            label_dict[item_l[1]].append(i)
        else:
            label_dict[item_l[1]] = [i]

    return dataset, label_dict


def get_train_loader(dataset, height, width, choice_c, batch_size, workers,
                     num_instances, iters, trainset=None):
    normalizer = T.Normalize(mean=[0.485, 0.456, 0.406],std=[0.229, 0.224, 0.225])
    train_transformer = T.Compose([
        T.Resize((height, width), interpolation=3),
        T.RandomHorizontalFlip(p=0.5),
        T.Pad(10),
        T.RandomCrop((height, width)),
        T.ToTensor(),
        normalizer,
        T.RandomErasing(probability=0.5, mean=[0.596, 0.558, 0.497])
    ])

    train_set = trainset #dataset.train if trainset is None else trainset
    rmgs_flag = num_instances > 0
    if rmgs_flag:
        sampler = RandomMultipleGallerySampler(train_set, num_instances, choice_c)
    else:
        sampler = None

    train_loader = IterLoader(
        DataLoader(Preprocessor(train_set, root=dataset.images_dir,
                                transform=train_transformer, mutual=True),
                   batch_size=batch_size, num_workers=workers, sampler=sampler,
                   shuffle=not rmgs_flag, pin_memory=True, drop_last=True), length=iters)

    # train_loader = IterLoader(
    #     DataLoader(UnsupervisedCamStylePreprocessor(train_set, root=dataset.images_dir, transform=train_transformer,
    #                                                 num_cam=dataset.num_cam,camstyle_dir=dataset.camstyle_dir, mutual=True),
    #                batch_size=batch_size, num_workers=0, sampler=sampler,#workers
    #                shuffle=not rmgs_flag, pin_memory=True, drop_last=True), length=iters)

    return train_loader


def get_test_loader(dataset, height, width, batch_size, workers, testset=None):
    normalizer = T.Normalize(mean=[0.485, 0.456, 0.406],std=[0.229, 0.224, 0.225])

    test_transformer = T.Compose([
        T.Resize((height, width), interpolation=3),
        T.ToTensor(),
        normalizer
    ])

    if (testset is None):
        testset = list(set(dataset.query) | set(dataset.gallery))

    test_loader = DataLoader(
        Preprocessor(testset, root=dataset.images_dir, transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    return test_loader

from UDAsbs.models.dsbn import convert_dsbn
from torch.nn import Parameter


def copy_state_dict(state_dict, model, strip=None):
    tgt_state = model.state_dict()
    copied_names = set()
    for name, param in state_dict.items():
        name = name.replace('module.', '')
        if strip is not None and name.startswith(strip):
            name = name[len(strip):]
        if name not in tgt_state:
            continue
        if isinstance(param, Parameter):
            param = param.data
        if param.size() != tgt_state[name].size():
            print('mismatch:', name, param.size(), tgt_state[name].size())
            continue
        tgt_state[name].copy_(param)
        copied_names.add(name)

    missing = set(tgt_state.keys()) - copied_names
    if len(missing) > 0:
        print("missing keys in state_dict:", missing)

    return model

def create_model(args, ncs, wopre=False):
    model_1 = models.create(args.arch, num_features=args.features, dropout=args.dropout,
                            num_classes=ncs)

    model_1_ema = models.create(args.arch, num_features=args.features, dropout=args.dropout,
                                num_classes=ncs)
    if not wopre:

        initial_weights = load_checkpoint(args.init_1)
        copy_state_dict(initial_weights['state_dict'], model_1)
        copy_state_dict(initial_weights['state_dict'], model_1_ema)
        print('load pretrain model:{}'.format(args.init_1))

    # adopt domain-specific BN
    convert_dsbn(model_1)
    convert_dsbn(model_1_ema)
    model_1.cuda()
    model_1_ema.cuda()
    model_1 = nn.DataParallel(model_1)
    model_1_ema = nn.DataParallel(model_1_ema)

    for i, cl in enumerate(ncs):
        exec('model_1_ema.module.classifier{}_{}.weight.data.copy_(model_1.module.classifier{}_{}.weight.data)'.format(i,cl,i,cl))

    return model_1, None, model_1_ema, None

def main():
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True

    main_worker(args)


class Optimizer:
    def __init__(self, target_label, m, dis_gt, t_loader,N, hc=3, ncl=None,  n_epochs=200,
                 weight_decay=1e-5, ckpt_dir='/',fc_len=3500):
        self.num_epochs = n_epochs
        self.momentum = 0.9
        self.weight_decay = weight_decay
        self.checkpoint_dir = ckpt_dir
        self.N=N
        self.resume = True
        self.checkpoint_dir = None
        self.writer = None
        # model stuff
        self.hc = len(ncl)#10
        self.K = ncl#3000
        self.K_c =[fc_len for _ in range(len(ncl))]
        self.model = m
        self.dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.L = [torch.LongTensor(target_label[i]).to(self.dev) for i in range(len(self.K))]
        self.nmodel_gpus = 4#len()
        self.pseudo_loader = t_loader#torch.utils.data.DataLoader(t_loader,batch_size=256)
        # can also be DataLoader with less aug.
        self.train_loader = t_loader
        self.lamb = 25#args.lamb # the parameter lambda in the SK algorithm
        self.cpu=True
        self.dis_gt=dis_gt
        dtype_='f64'
        if dtype_ == 'f32':
            self.dtype = torch.float32 if not self.cpu else np.float32
        else:
            self.dtype = torch.float64 if not self.cpu else np.float64

        self.outs = self.K
        # activations of previous to last layer to be saved if using multiple heads.
        self.presize =  2048#4096 #

    def optimize_labels(self):
        if self.cpu:
            sk.cpu_sk(self)
        else:
            sk.gpu_sk(self)

        # save Label-assignments: optional
        # torch.save(self.L, os.path.join(self.checkpoint_dir, 'L', str(niter) + '_L.gz'))

        # free memory
        data = 0
        self.PS = 0

        return self.L

import collections


def func(x, a, b, c):
    return a * np.exp(-b * x) + c

def write_sta_im(train_loader):
    label2num=collections.defaultdict(int)
    save_label=[]
    for x in train_loader:
        label2num[x[1]]+=1
        save_label.append(x[1])
    labels=sorted(label2num.items(),key=lambda item:item[1])[::-1]
    num = [j for i, j in labels]
    distribution = np.array(num)/len(train_loader)

    return num,save_label
def print_cluster_acc(label_dict,target_label_tmp):
    num_correct = 0
    for pid in label_dict:
        pid_index = np.asarray(label_dict[pid])
        pred_label = np.argmax(np.bincount(target_label_tmp[pid_index]))
        num_correct += (target_label_tmp[pid_index] == pred_label).astype(np.float32).sum()
    cluster_accuracy = num_correct / len(target_label_tmp)
    print(f'cluster accucary: {cluster_accuracy:.3f}')


class uncer(object):
    def __init__(self):
        self.sm = torch.nn.Softmax(dim=1)
        self.log_sm = torch.nn.LogSoftmax(dim=1)
        # self.cross_batch=CrossBatchMemory()
        self.kl_distance = nn.KLDivLoss(reduction='none')
    def kl_cal(self,pred1,pred1_ema):
        variance = torch.sum(self.kl_distance(self.log_sm(pred1),
                                              self.sm(pred1_ema.detach())), dim=1)
        exp_variance = torch.exp(-variance)
        return exp_variance


def main_worker(args):
    global start_epoch, best_mAP

    cudnn.benchmark = True

    sys.stdout = Logger(osp.join(args.logs_dir, 'log.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    # Create data loaders
    iters = args.iters if (args.iters > 0) else None
    ncs = [int(x) for x in args.ncs.split(',')]
    # ncs_dbscan=ncs.copy()
    dataset_target, label_dict = get_data(args.dataset_target, args.data_dir, len(ncs))
    test_loader_target = get_test_loader(dataset_target, args.height, args.width, args.batch_size, args.workers)
    tar_cluster_loader = get_test_loader(dataset_target, args.height, args.width, args.batch_size, args.workers,
                                         testset=dataset_target.train)


    dataset_source, _ = get_data(args.dataset_source, args.data_dir, len(ncs))
    sour_cluster_loader = get_test_loader(dataset_source, args.height, args.width, args.batch_size, args.workers,
                                          testset=dataset_source.train)
    train_loader_source = get_train_loader(dataset_source, args.height, args.width, 0, args.batch_size, args.workers,
                                           args.num_instances, args.iters, dataset_source.train)

    source_classes = dataset_source.num_train_pids
    distribution,_ = write_sta_im(dataset_source.train)



    fc_len = 3500
    model_1, _, model_1_ema, _ = create_model(args, [fc_len for _ in range(len(ncs))])
    # print(model_1)


    epoch = 0
    target_features_dict, _ = extract_features(model_1_ema, tar_cluster_loader, print_freq=100)
    target_features = F.normalize(torch.stack(list(target_features_dict.values())), dim=1)

    # Calculate distance
    print('==> Create pseudo labels for unlabeled target domain')

    rerank_dist = compute_jaccard_distance(target_features, k1=args.k1, k2=args.k2)
    del target_features
    if (epoch == 0):
        # DBSCAN cluster
        eps = 0.6  # 0.6
        print('Clustering criterion: eps: {:.3f}'.format(eps))
        cluster = DBSCAN(eps=eps, min_samples=4, metric='precomputed', n_jobs=-1)

    # select & cluster images as training set of this epochs
    pseudo_labels = cluster.fit_predict(rerank_dist)

    # num_ids = len(set(pseudo_labels)) - (1 if -1 in pseudo_labels else 0)
    plabel=[]
    new_dataset=[]
    for i, (item, label) in enumerate(zip(dataset_target.train, pseudo_labels)):

        if label == -1:
            continue
        plabel.append(label)
        new_dataset.append((item[0], label, item[-1]))

    target_label = [plabel]
    ncs = [len(set(plabel)) + 1]
    print('new class are {}, length of new dataset is {}'.format(ncs, len(new_dataset)))
    model_1.module.classifier0_3500 = nn.Linear(2048, ncs[0]+source_classes, bias=False).cuda()
    model_1_ema.module.classifier0_3500 = nn.Linear(2048, ncs[0]+source_classes, bias=False).cuda()

    model_1.module.classifier3_0_3500 = nn.Linear(1024, ncs[0]+source_classes, bias=False).cuda()
    model_1_ema.module.classifier3_0_3500 = nn.Linear(1024, ncs[0]+source_classes, bias=False).cuda()
    print(model_1.module.classifier0_3500)
    # if epoch !=0:
    #     model_1.module.classifier0_3500.weight.data.copy_(torch.from_numpy(normalize(target_centers,axis=1)).float().cuda())
    #     model_1_ema.module.classifier0_3500.weight.data.copy_(torch.from_numpy(normalize(target_centers,axis=1)).float().cuda())



    # Initialize source-domain class centroids
    print("==> Initialize source-domain class centroids in the hybrid memory")
    source_features, _ = extract_features(model_1, sour_cluster_loader, print_freq=50)
    sour_fea_dict = collections.defaultdict(list)
    print("==> Ending source-domain class centroids in the hybrid memory")
    for f, pid, _ in sorted(dataset_source.train):
        sour_fea_dict[pid].append(source_features[f].unsqueeze(0))
    source_centers = [torch.cat(sour_fea_dict[pid], 0).mean(0) for pid in sorted(sour_fea_dict.keys())]
    source_centers = torch.stack(source_centers, 0)
    source_centers = F.normalize(source_centers, dim=1)
    del sour_fea_dict, source_features, sour_cluster_loader

    # Evaluator
    evaluator_1 = Evaluator(model_1)
    evaluator_1_ema = Evaluator(model_1_ema)


    clusters = [args.num_clusters] * args.epochs# TODO: dropout clusters

    k_memory=8192
    contrast = onlinememory(2048, len(new_dataset),sour_numclass=source_classes,K=k_memory+source_classes,
                             index2label=target_label, choice_c=args.choice_c, T=0.07,
                             use_softmax=True).cuda()
    contrast.index_memory = torch.cat((torch.arange(source_classes), -1*torch.ones(k_memory).long()), dim=0).cuda()
    contrast.memory = torch.cat((source_centers, torch.rand(k_memory, 2048)), dim=0).cuda()

    tar_selflabel_loader = get_test_loader(dataset_target, args.height, args.width, args.batch_size, args.workers,
                                         testset=new_dataset)

    o = Optimizer(target_label, dis_gt=distribution, m=model_1, ncl=ncs,
                  t_loader=tar_selflabel_loader, N=len(new_dataset), fc_len=fc_len)

    uncertainty=collections.defaultdict(list)
    print("Training begining~~~~~~!!!!!!!!!")
    for epoch in range(len(clusters)):

        iters_ = 300 if epoch  % 1== 0 else iters
        if epoch % 6 == 0 and epoch !=0:
            target_features_dict, _ = extract_features(model_1_ema, tar_cluster_loader, print_freq=50)

            target_features = torch.stack(list(target_features_dict.values()))
            target_features = F.normalize(target_features, dim=1)

            print('==> Create pseudo labels for unlabeled target domain with')
            rerank_dist = compute_jaccard_distance(target_features, k1=args.k1, k2=args.k2)

            # select & cluster images as training set of this epochs
            pseudo_labels = cluster.fit_predict(rerank_dist)
            plabel = []

            new_dataset = []

            for i, (item, label) in enumerate(zip(dataset_target.train, pseudo_labels)):
                if label == -1:continue
                plabel.append(label)
                new_dataset.append((item[0], label, item[-1]))

            target_label = [plabel]
            ncs = [len(set(plabel)) + 1]

            tar_selflabel_loader = get_test_loader(dataset_target, args.height, args.width, args.batch_size, args.workers,
                                                 testset=new_dataset)
            o = Optimizer(target_label, dis_gt=distribution, m=model_1, ncl=ncs,
                          t_loader=tar_selflabel_loader, N=len(new_dataset),fc_len=fc_len)


            contrast.index_memory = torch.cat((torch.arange(source_classes), -1 * torch.ones(k_memory).long()),
                                              dim=0).cuda()

            model_1.module.classifier0_3500 = nn.Linear(2048, ncs[0] + source_classes, bias=False).cuda()
            model_1_ema.module.classifier0_3500 = nn.Linear(2048, ncs[0] + source_classes, bias=False).cuda()

            model_1.module.classifier3_0_3500 = nn.Linear(1024, ncs[0] + source_classes, bias=False).cuda()
            model_1_ema.module.classifier3_0_3500 = nn.Linear(1024, ncs[0] + source_classes, bias=False).cuda()
            print(model_1.module.classifier0_3500)
            # if epoch !=0:
            #     model_1.module.classifier0_3500.weight.data.copy_(torch.from_numpy(normalize(target_centers,axis=1)).float().cuda())
            #     model_1_ema.module.classifier0_3500.weight.data.copy_(torch.from_numpy(normalize(target_centers,axis=1)).float().cuda())

        target_label_o = o.L
        target_label = [list(np.asarray(target_label_o[0].data.cpu())+source_classes)]
        contrast.index2label = [[i for i in range(source_classes)] + target_label[0]]

        # change pseudo labels
        for i in range(len(new_dataset)):
            new_dataset[i] = list(new_dataset[i])
            for j in range(len(ncs)):
                new_dataset[i][j+1] = int(target_label[j][i])
            new_dataset[i] = tuple(new_dataset[i])

        cc=args.choice_c#(args.choice_c+1)%len(ncs)
        train_loader_target = get_train_loader(dataset_target, args.height, args.width, cc,
                                               args.batch_size, args.workers, args.num_instances, iters_, new_dataset)

        # Optimizer
        params = []
        flag = 1.0
        # if 20<epoch<=40 or 60<epoch<=80 or 120<epoch:
        #     flag=0.1
        # else:
        #     flag=1.0

        for key, value in model_1.named_parameters():
            if not value.requires_grad:
                print(key)
                continue
            params += [{"params": [value], "lr": args.lr*flag, "weight_decay": args.weight_decay}]

        optimizer = torch.optim.Adam(params)

        # Trainer
        trainer = DbscanBaseTrainer_unc_ema(model_1, model_1_ema, contrast, None,None,
                             num_cluster=ncs, c_name=ncs,alpha=args.alpha, fc_len=fc_len,
                                            source_classes=source_classes, uncer_mode=args.uncer_mode)


        train_loader_target.new_epoch()
        train_loader_source.new_epoch()


        trainer.train(epoch, train_loader_target, train_loader_source, optimizer, args.choice_c,
                                    lambda_tri=args.lambda_tri, lambda_ct=args.lambda_ct, lambda_reg=args.lambda_reg,
                                    print_freq=args.print_freq, train_iters=iters_,uncertainty_d=uncertainty)


        def save_model(model_ema, is_best, best_mAP, mid):
            save_checkpoint({
                'state_dict': model_ema.state_dict(),
                'epoch': epoch + 1,
                'best_mAP': best_mAP,
            }, is_best, fpath=osp.join(args.logs_dir, 'model' + str(mid) + '_checkpoint.pth.tar'))
        if epoch==20:
            args.eval_step=2
        elif epoch==40:
            args.eval_step=1
        if ((epoch + 1) % args.eval_step == 0 or (epoch == args.epochs - 1)):
            mAP_1 = 0#evaluator_1.evaluate(test_loader_target, dataset_target.query, dataset_target.gallery,
                     #          cmc_flag=False)


            mAP_2 = evaluator_1_ema.evaluate(test_loader_target, dataset_target.query, dataset_target.gallery,
                                             cmc_flag=False)
            is_best = (mAP_1 > best_mAP) or (mAP_2 > best_mAP)
            best_mAP = max(mAP_1, mAP_2, best_mAP)
            save_model(model_1, (is_best), best_mAP, 1)
            save_model(model_1_ema, (is_best and (mAP_1 <= mAP_2)), best_mAP, 2)

            print('\n * Finished epoch {:3d}  model no.1 mAP: {:5.1%} model no.2 mAP: {:5.1%}  best: {:5.1%}{}\n'.
                  format(epoch, mAP_1, mAP_2, best_mAP, ' *' if is_best else ''))



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="MMT Training")
    # data
    parser.add_argument('-st', '--dataset-source', type=str, default='market1501',
                        choices=datasets.names())
    parser.add_argument('-tt', '--dataset-target', type=str, default='dukemtmc',
                        choices=datasets.names())
    parser.add_argument('-b', '--batch-size', type=int, default=64)
    parser.add_argument('-j', '--workers', type=int, default=8)
    parser.add_argument('--choice_c', type=int, default=0)
    parser.add_argument('--num-clusters', type=int, default=700)
    parser.add_argument('--ncs', type=str, default='60')

    parser.add_argument('--k1', type=int, default=30,
                        help="hyperparameter for jaccard distance")
    parser.add_argument('--k2', type=int, default=6,
                        help="hyperparameter for jaccard distance")

    parser.add_argument('--height', type=int, default=256,
                        help="input height")
    parser.add_argument('--width', type=int, default=128,
                        help="input width")

    parser.add_argument('--num-instances', type=int, default=4,
                        help="each minibatch consist of "
                             "(batch_size // num_instances) identities, and "
                             "each identity has num_instances instances, "
                             "default: 0 (NOT USE)")
    # model
    parser.add_argument('-a', '--arch', type=str, default='resnet50_multi',
                        choices=models.names())
    parser.add_argument('--features', type=int, default=0)
    parser.add_argument('--dropout', type=float, default=0)
    # optimizer

    parser.add_argument('--lr', type=float, default=0.00035,
                        help="learning rate of new parameters, for pretrained "
                             "parameters it is 10 times smaller than this")
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--alpha', type=float, default=0.999)
    parser.add_argument('--moving-avg-momentum', type=float, default=0)
    parser.add_argument('--weight-decay', type=float, default=5e-4)
    parser.add_argument('--soft-ce-weight', type=float, default=0.5)
    parser.add_argument('--soft-tri-weight', type=float, default=0.8)
    parser.add_argument('--epochs', type=int, default=400)
    parser.add_argument('--iters', type=int, default=300)

    parser.add_argument('--lambda-value', type=float, default=0)
    # training configs

    parser.add_argument('--rr-gpu', action='store_true',
                        help="use GPU for accelerating clustering")
    # parser.add_argument('--init-1', type=str, default='logs/personxTOpersonxval/resnet_ibn50a-pretrain-1_gem_RA//model_best.pth.tar', metavar='PATH')
    parser.add_argument('--init-1', type=str,
                        default='logs/market1501TOdukemtmc/resnet50-pretrain-1005/model_best.pth.tar',
                        metavar='PATH')

    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--print-freq', type=int, default=100)
    parser.add_argument('--eval-step', type=int, default=5)
    parser.add_argument('--n-jobs', type=int, default=8)
    # path
    working_dir = osp.dirname(osp.abspath(__file__))
    parser.add_argument('--data-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'data'))
    parser.add_argument('--logs-dir', type=str, metavar='PATH',
                        default=osp.join(working_dir, 'logs/d2m_baseline/tmp'))

    parser.add_argument('--lambda-tri', type=float, default=1.0)
    parser.add_argument('--lambda-reg', type=float, default=1.0)
    parser.add_argument('--lambda-ct', type=float, default=0.05)
    parser.add_argument('--uncer-mode', type=float, default=0)#0 mean 1 max 2 min

    print("======mmt_train_dbscan_self-labeling=======")


    main()