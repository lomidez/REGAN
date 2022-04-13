# -*- coding:utf-8 -*-

import os
import random
import math
import argparse
import tqdm
import numpy as np
import matplotlib.pyplot as plt
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable

from generator import Generator
from discriminator import LSTMDiscriminator
from annex_network import AnnexNetwork, LSTMAnnexNetwork
from rollout import Rollout
from data_iter import GenDataIter, DisDataIter
from data_loader import DataLoader

from utils import *
from loss import *
from helpers import *


isDebug = True

# ================== Parameter Definition =================

# BASIC TRAINING PARAMETERS
THC_CACHING_ALLOCATOR = 0
SEED = 88
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
BATCH_SIZE = 128

# DATA
GENERATED_NUM = 10000
SPACES = False # What kind of data do you want to work on?
SEQ_LEN = 3 # 15 for SPACES = True, 3 or 15 for SPACES = False
if SPACES:
    POSITIVE_FILE = 'data/math_equation_data.txt'
else:
    POSITIVE_FILE = 'data/math_equation_data_no_spaces.txt'    
if SEQ_LEN == 3:
    POSITIVE_FILE = 'data/math_equation_data_3.txt'
NEGATIVE_FILE = 'gene.data' if SEQ_LEN == 15 else 'gene_3.data'
EVAL_FILE = 'eval.data' if SEQ_LEN == 15  else 'eval_3.data'
if SPACES:
    VOCAB_SIZE = 6
else:
    VOCAB_SIZE = 5

# PRE-TRAINING
# Loading weights currently not supported if SPACES = True
MLE = False # If True, do pre-training, otherwise, load weights
if SEQ_LEN == 3:
    weights_path = "checkpoints/MLE_space_False_length_3_preTrainG_epoch_0_official.pth"
else:
    weights_path = "checkpoints/MLE_space_False_length_15_preTrainG_epoch_2_official.pth"
PRE_EPOCH_GEN = 3 if isDebug else 120 # can be a decimal number
PRE_EPOCH_DIS = 0 if isDebug else 5
PRE_ITER_DIS = 0 if isDebug else 3

# ADVERSARIAL TRAINING
GD = "RELAX" # "MLE" or REINFORCE" or "REBAR" or "RELAX"
if GD == "MLE":
    TOTAL_EPOCHS = 0
CHECK_VARIANCE = True
if GD == "RELAX":
    CHECK_VARIANCE = True
UPDATE_RATE = 0.8
TOTAL_EPOCHS = 3 # can be a decimal number
TOTAL_BATCH = int(TOTAL_EPOCHS * int(GENERATED_NUM/BATCH_SIZE))
G_STEPS = 1 if isDebug else 1
D_STEPS = 1 if isDebug else 4
D_EPOCHS = 1 if isDebug else 2

# GENERATOR
g_emb_dim = 32
g_hidden_dim = 32
g_sequence_len = SEQ_LEN

# DISCRIMINATOR
d_emb_dim = 64
if SEQ_LEN == 3:
    d_filter_sizes = [1, 2, 3]
    d_num_filters = [100, 200, 200]
else:
    d_filter_sizes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15]
    d_num_filters = [100, 200, 200, 200, 200, 100, 100, 100, 100, 100, 160]
d_dropout = 0.75
d_num_class = 2
d_lstm_hidden_dim = 32

# ANNEX NETWORK
c_lstm_hidden_dim = 32
if SEQ_LEN == 3:
    c_filter_sizes = [1, 3]
    c_num_filters = [100, 200]
else: 
    c_filter_sizes = [1, 3, 5, 7, 9, 15]
    c_num_filters = [100, 200, 200, 200, 100, 100]
    
# OTHER HYPER-PARAMETERS
DEFAULT_ETA = 1 #for REBAR only. Note: Naive value, in paper they estimate value
DEFAULT_TEMPERATURE = 1


def main(opt):

    cuda = opt.cuda; visualize = opt.visualize
    print(f"cuda = {cuda}, visualize = {opt.visualize}")
    if visualize:
        if PRE_EPOCH_GEN > 0: pretrain_G_score_logger = VisdomPlotLogger('line', opts={'title': 'Pre-train G Goodness Score'})
        if PRE_EPOCH_DIS > 0: pretrain_D_loss_logger = VisdomPlotLogger('line', opts={'title': 'Pre-train D Loss'})
        adversarial_G_score_logger = VisdomPlotLogger('line', opts={'title': f'Adversarial G {GD} Goodness Score',
                                                      'Y': '{0, 13}', 'X': '{0, TOTAL_BATCH}' })
        if CHECK_VARIANCE: G_variance_logger = VisdomPlotLogger('line', opts={'title': f'Adversarial G {GD} Variance'})
        G_text_logger = VisdomTextLogger(update_type='APPEND')
        adversarial_D_loss_logger = VisdomPlotLogger('line', opts={'title': 'Adversarial Batch D Loss'})

    # Define Networks
    generator = Generator(VOCAB_SIZE, g_emb_dim, g_hidden_dim, cuda)
    n_gen = Variable(torch.Tensor([get_n_params(generator)]))
    use_cuda = False
    if cuda:
        n_gen = n_gen.cuda()
        use_cuda = True
    print('Number of parameters in the generator: {}'.format(n_gen))
    discriminator = LSTMDiscriminator(d_num_class, VOCAB_SIZE, d_lstm_hidden_dim, use_cuda)
    c_phi_hat = AnnexNetwork(d_num_class, VOCAB_SIZE, d_emb_dim, c_filter_sizes, c_num_filters, d_dropout, BATCH_SIZE, g_sequence_len)
    if cuda:
        generator = generator.cuda()
        discriminator = discriminator.cuda()
        c_phi_hat = c_phi_hat.cuda()

    # Generate toy data using target lstm
    print('Generating data ...')
    
    # Load data from file
    gen_data_iter = DataLoader(POSITIVE_FILE, BATCH_SIZE)

    gen_criterion = nn.NLLLoss(size_average=False)
    gen_optimizer = optim.Adam(generator.parameters())
    if cuda:
        gen_criterion = gen_criterion.cuda()

    # Pretrain Generator using MLE        
    pre_train_scores = []
    if MLE:    
        print('Pretrain with MLE ...')
        for epoch in range(int(np.ceil(PRE_EPOCH_GEN))):
            loss = train_epoch(generator, gen_data_iter, gen_criterion, gen_optimizer, PRE_EPOCH_GEN, epoch, cuda)
            print('Epoch [%d] Model Loss: %f'% (epoch, loss))
            samples = generate_samples(generator, BATCH_SIZE, GENERATED_NUM, EVAL_FILE)
            eval_iter = DataLoader(EVAL_FILE, BATCH_SIZE)
            generated_string = eval_iter.convert_to_char(samples)
            print(generated_string)
            eval_score = get_data_goodness_score(generated_string, SPACES)
            if SPACES == False:
                kl_score = get_data_freq(generated_string)
            else:
                kl_score = -1
            freq_score = get_char_freq(generated_string, SPACES)
            pre_train_scores.append(eval_score)
            print('Epoch [%d] Generation Score: %f' % (epoch, eval_score))
            print('Epoch [%d] KL Score: %f' % (epoch, kl_score))
            print('Epoch [{}] Character distribution: {}'.format(epoch, list(freq_score)))
            
            torch.save(generator.state_dict(), f"checkpoints/MLE_space_{SPACES}_length_{SEQ_LEN}_preTrainG_epoch_{epoch}.pth")
            
            if visualize:
                pretrain_G_score_logger.log(epoch, eval_score)
    else:
        generator.load_state_dict(torch.load(weights_path))

    # Finishing training with MLE  
    if GD == "MLE":   
        for epoch in range(3*int(GENERATED_NUM/BATCH_SIZE)):
            loss = train_epoch_batch(generator, gen_data_iter, gen_criterion, gen_optimizer, PRE_EPOCH_GEN, epoch, int(GENERATED_NUM/BATCH_SIZE), cuda)
            print('Epoch [%d] Model Loss: %f'% (epoch, loss))
            samples = generate_samples(generator, BATCH_SIZE, GENERATED_NUM, EVAL_FILE)
            eval_iter = DataLoader(EVAL_FILE, BATCH_SIZE)
            generated_string = eval_iter.convert_to_char(samples)
            print(generated_string)
            eval_score = get_data_goodness_score(generated_string, SPACES)
            if SPACES == False:
                kl_score = get_data_freq(generated_string)
            else:
                kl_score = -1
            freq_score = get_char_freq(generated_string, SPACES)
            pre_train_scores.append(eval_score)
            print('Epoch [%d] Generation Score: %f' % (epoch, eval_score))
            print('Epoch [%d] KL Score: %f' % (epoch, kl_score))
            print('Epoch [{}] Character distribution: {}'.format(epoch, list(freq_score)))
            
            torch.save(generator.state_dict(), f"checkpoints/MLE_space_{SPACES}_length_{SEQ_LEN}_preTrainG_epoch_{epoch}.pth")
            
            if visualize:
                pretrain_G_score_logger.log(epoch, eval_score)

    # Pretrain Discriminator
    dis_criterion = nn.NLLLoss(size_average=False)
    dis_optimizer = optim.Adam(discriminator.parameters())
    if opt.cuda:
        dis_criterion = dis_criterion.cuda()
    print('Pretrain Discriminator ...')
    for epoch in range(PRE_EPOCH_DIS):
        samples = generate_samples(generator, BATCH_SIZE, GENERATED_NUM, NEGATIVE_FILE)
        dis_data_iter = DisDataIter(POSITIVE_FILE, NEGATIVE_FILE, BATCH_SIZE, SEQ_LEN)
        for _ in range(PRE_ITER_DIS):
            loss = train_epoch(discriminator, dis_data_iter, dis_criterion, dis_optimizer, 1, 1, cuda)
            print('Epoch [%d], loss: %f' % (epoch, loss))
            if visualize:
                pretrain_D_loss_logger.log(epoch, loss)

    # Adversarial Training 
    rollout = Rollout(generator, UPDATE_RATE)
    print('#####################################################')
    print('Start Adversarial Training...\n')
    
    gen_gan_loss = GANLoss()
    gen_gan_optm = optim.Adam(generator.parameters())
    if cuda:
        gen_gan_loss = gen_gan_loss.cuda()
    gen_criterion = nn.NLLLoss(size_average=False)
    if cuda:
        gen_criterion = gen_criterion.cuda()
    
    dis_criterion = nn.NLLLoss(size_average=False)
    dis_criterion_bce = nn.BCELoss()
    dis_optimizer = optim.Adam(discriminator.parameters())
    if cuda:
        dis_criterion = dis_criterion.cuda()
    
    c_phi_hat_loss = VarianceLoss()
    if cuda:
        c_phi_hat_loss = c_phi_hat_loss.cuda()
    c_phi_hat_optm = optim.Adam(c_phi_hat.parameters())

    gen_scores = pre_train_scores
    
    for total_batch in range(TOTAL_BATCH):
        # Train the generator for one step
        for it in range(G_STEPS):
            samples = generator.sample(BATCH_SIZE, g_sequence_len)
            # samples has size (BS, sequence_len)
            # Construct the input to the generator, add zeros before samples and delete the last column
            zeros = torch.zeros((BATCH_SIZE, 1)).type(torch.LongTensor)
            if samples.is_cuda:
                zeros = zeros.cuda()
            inputs = Variable(torch.cat([zeros, samples.data], dim = 1)[:, :-1].contiguous())
            targets = Variable(samples.data).contiguous().view((-1,))
            if opt.cuda:
                inputs=inputs.cuda()
                targets=targets.cuda()
            # Calculate the reward
            rewards = rollout.get_reward(samples, discriminator, VOCAB_SIZE, cuda)
            rewards = Variable(torch.Tensor(rewards))
            if cuda:
                rewards = torch.exp(rewards.cuda()).contiguous().view((-1,))
            rewards = torch.exp(rewards)
            # rewards has size (BS)
            prob = generator.forward(inputs)
            # prob has size (BS*sequence_len, VOCAB_SIZE)
            # 3.a
            theta_prime = g_output_prob(prob)
            # theta_prime has size (BS*sequence_len, VOCAB_SIZE)
            # 3.e and f
            c_phi_z_ori, c_phi_z_tilde_ori = c_phi_out(GD, c_phi_hat, theta_prime, discriminator, temperature=DEFAULT_TEMPERATURE, eta=DEFAULT_ETA, cuda=cuda)
            c_phi_z_ori = torch.exp(c_phi_z_ori)
            c_phi_z_tilde_ori = torch.exp(c_phi_z_tilde_ori)
            c_phi_z = torch.sum(c_phi_z_ori[:,1])/BATCH_SIZE
            c_phi_z_tilde = -torch.sum(c_phi_z_tilde_ori[:,1])/BATCH_SIZE
            if opt.cuda:
                c_phi_z = c_phi_z.cuda()
                c_phi_z_tilde = c_phi_z_tilde.cuda()
                c_phi_hat = c_phi_hat.cuda()
            # 3.i
            grads = []
            first_term_grads = []
            # 3.h optimization step
            # first, empty the gradient buffers
            gen_gan_optm.zero_grad()
            # first, re arrange prob
            new_prob = prob.view((BATCH_SIZE, g_sequence_len, VOCAB_SIZE))
            # 3.g new gradient loss for relax 
            batch_i_grads_1 = gen_gan_loss.forward_reward_grads(samples, new_prob, rewards, generator, BATCH_SIZE, g_sequence_len, VOCAB_SIZE, cuda)
            batch_i_grads_2 = gen_gan_loss.forward_reward_grads(samples, new_prob, c_phi_z_tilde_ori[:,1], generator, BATCH_SIZE, g_sequence_len, VOCAB_SIZE, cuda)
            # batch_i_grads_1 and batch_i_grads_2 should be of length BATCH SIZE of arrays of all the gradients
            # # 3.i
            batch_grads = batch_i_grads_1
            if GD != "REINFORCE":
                for i in range(len(batch_i_grads_1)):
                    for j in range(len(batch_i_grads_1[i])):
                        batch_grads[i][j] = torch.add(batch_grads[i][j], (-1)*batch_i_grads_2[i][j])
            # batch_grads should be of length BATCH SIZE
            grads.append(batch_grads)
            # NOW, TRAIN THE GENERATOR
            generator.zero_grad()
            for i in range(g_sequence_len):
                # 3.g new gradient loss for relax 
                cond_prob = gen_gan_loss.forward_reward(i, samples, new_prob, rewards, BATCH_SIZE, g_sequence_len, VOCAB_SIZE, cuda)
                c_term = gen_gan_loss.forward_reward(i, samples, new_prob, c_phi_z_tilde_ori[:,1], BATCH_SIZE, g_sequence_len, VOCAB_SIZE, cuda)
                if GD != "REINFORCE":
                    cond_prob = torch.add(cond_prob, (-1)*c_term)
                new_prob[:, i, :].backward(cond_prob, retain_graph=True)
            # 3.h - still training the generator, with the last two terms of the RELAX equation
            if GD != "REINFORCE":
                c_phi_z.backward(retain_graph=True)
                c_phi_z_tilde.backward(retain_graph=True)
            gen_gan_optm.step()
            # 3.i
            if CHECK_VARIANCE:
                # c_phi_z term
                partial_grads = []
                for j in range(BATCH_SIZE):
                    generator.zero_grad()
                    ##############################################################
                    #c_phi_z_ori[j,1].backward(retain_graph=True)
                    c_phi_z_ori[j,1].backward()
                    j_grads = []
                    for p in generator.parameters():
                        j_grads.append(p.grad.clone())
                    partial_grads.append(j_grads)
                grads.append(partial_grads)
                # c_phi_z_tilde term
                partial_grads = []
                for j in range(BATCH_SIZE):
                    generator.zero_grad()
                    ##############################################################
                    #c_phi_z_tilde_ori[j,1].backward(retain_graph=True)
                    c_phi_z_tilde_ori[j,1].backward()
                    j_grads = []
                    for p in generator.parameters():
                        j_grads.append(-1*p.grad.clone())
                    partial_grads.append(j_grads)
                grads.append(partial_grads)
                # Uncomment the below code if you want to check gradients
                """
                print('1st contribution to the gradient')
                print(grads[0][0][6])
                print('2nd contribution to the gradient')
                print(grads[1][0][6])
                print('3rd contribution to the gradient')
                print(grads[2][0][6])
                """
                #grads should be of length 3
                #grads[0] should be of length BATCH SIZE
                # 3.j
                all_grads = grads[0]
                if GD != "REINFORCE":
                    for i in range(len(grads[0])):
                        for j in range(len(grads[0][i])):
                            all_grads[i][j] = torch.add(torch.add(all_grads[i][j], grads[1][i][j]), grads[2][i][j])
                # all_grads should be of length BATCH_SIZE
                c_phi_hat_optm.zero_grad()
                var_loss = c_phi_hat_loss.forward(all_grads, cuda)  #/n_gen
                true_variance = c_phi_hat_loss.forward_variance(all_grads, cuda)
                var_loss.backward()
                c_phi_hat_optm.step()
                print('Batch [{}] Estimate of the variance of the gradient at step {}: {}'.format(total_batch, it, true_variance[0]))
                if visualize:
                    G_variance_logger.log((total_batch + it), true_variance[0])

        # Evaluate the quality of the Generator outputs
        if total_batch % 1 == 0 or total_batch == TOTAL_BATCH - 1:
                samples = generate_samples(generator, BATCH_SIZE, GENERATED_NUM, EVAL_FILE)
                eval_iter = DataLoader(EVAL_FILE, BATCH_SIZE)
                generated_string = eval_iter.convert_to_char(samples)
                print(generated_string)
                eval_score = get_data_goodness_score(generated_string, SPACES)
                if SPACES == False:
                    kl_score = get_data_freq(generated_string)
                else:
                    kl_score = -1
                freq_score = get_char_freq(generated_string, SPACES)
                gen_scores.append(eval_score)
                print('Batch [%d] Generation Score: %f' % (total_batch, eval_score))
                print('Batch [%d] KL Score: %f' % (total_batch, kl_score))
                print('Epoch [{}] Character distribution: {}'.format(total_batch, list(freq_score)))

                #Checkpoint & Visualize
                if total_batch % 10 == 0 or total_batch == TOTAL_BATCH -1:
                    torch.save(generator.state_dict(), f'checkpoints/{GD}_G_space_{SPACES}_pretrain_{PRE_EPOCH_GEN}_batch_{total_batch}.pth')
                if visualize:
                    [G_text_logger.log(line) for line in generated_string]
                    adversarial_G_score_logger.log(total_batch, eval_score)

        # Train the discriminator
        batch_G_loss = 0.0

        for b in range(D_EPOCHS):

            for data, _ in gen_data_iter:

                data = Variable(data)
                real_data = convert_to_one_hot(data, VOCAB_SIZE, cuda)
                real_target = Variable(torch.ones((data.size(0), 1)))
                samples = generator.sample(data.size(0), g_sequence_len) # bs x seq_len
                fake_data = convert_to_one_hot(samples, VOCAB_SIZE, cuda) # bs x seq_len x vocab_size
                fake_target = Variable(torch.zeros((data.size(0), 1)))
                
                if cuda:
                    real_target = real_target.cuda()
                    fake_target = fake_target.cuda()
                    real_data = real_data.cuda()
                    fake_data = fake_data.cuda()
                
                real_pred = torch.exp(discriminator(real_data)[:, 1])
                fake_pred = torch.exp(discriminator(fake_data)[:, 1])

                D_real_loss = dis_criterion_bce(real_pred, real_target)
                D_fake_loss = dis_criterion_bce(fake_pred, fake_target)
                D_loss = D_real_loss + D_fake_loss
                dis_optimizer.zero_grad()
                D_loss.backward()
                dis_optimizer.step()

            gen_data_iter.reset()

            print('Batch [{}] Discriminator Loss at step and epoch {}: {}'.format(total_batch, b, D_loss.data[0]))

        if visualize:
            adversarial_D_loss_logger.log(total_batch, D_loss.data[0])

    if not visualize:
        plt.plot(gen_scores)
        plt.ylim((0, 13))
        plt.title('{}_after_{}_epochs_of_pretraining'.format(GD, PRE_EPOCH_GEN))
        plt.show()


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Training Parameter')
    parser.add_argument('--visualize', action='store_true', help='Enables Visdom')
    parser.add_argument('--cuda', action='store', default=None, type=int)
    opt = parser.parse_args()
    if opt.cuda is not None and opt.cuda >= 0:
        if torch.cuda.is_available():
            torch.cuda.set_device(opt.cuda)
            opt.cuda = True
        else:
            opt.cuda = False

    try:
        from eval.helper import *
        from eval.BLEU_score import *
        from visdom import Visdom
        import torchnet as tnt
        from torchnet.engine import Engine
        from torchnet.logger import VisdomPlotLogger, VisdomTextLogger, VisdomLogger
        canVisualize = True
    except ImportError as ie:
        eprint("Could not import vizualization imports. ")
        canVisualize = False

    opt.visualize = True if (opt.visualize and canVisualize) else False
    main(opt)
