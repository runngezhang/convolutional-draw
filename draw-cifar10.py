#!/usr/bin/env python

""""
Simple implementation of Convolutional DRAW in TensorFlow

Example Usage: 
    python draw.py --data_dir=/tmp/draw --read_attn=True --write_attn=True

Author: Volodymyr Kuleshov, based on code by Eric Jang and @iwyoo
"""

import tensorflow as tf
import numpy as np
import os

from convlstm import ConvLSTMCell

tf.flags.DEFINE_string("data_dir", "", "")
FLAGS = tf.flags.FLAGS

## MODEL PARAMETERS ## 

A,B = 32,32 # image width,height

img_size = B*A # the canvas size

n_chan_x = 3 # num of channels in the images
n_chan_r = n_chan_x * 2 # contains mean + log-stdv
n_chan_z = 12 # num of channels in the latent variables
n_chan_hd = 320 # num of channels in decoder hidden state
n_chan_he = 320 # num of channels in encoder hidden state

T=32 # generation sequence length
batch_size=32 # training minibatch size
train_iters=10000
learning_rate=1e-4 # learning rate for optimizer
eps=1e-6 # epsilon for numerical stability

## BUILD MODEL ## 

DO_SHARE=None # workaround for variable_scope(reuse=True)

x = tf.placeholder(tf.float32,shape=(batch_size,A,B,n_chan_x)) # input (batch_size * img_size)
e=tf.random_normal((batch_size,A/2,B/2,n_chan_z), mean=0, stddev=1) # Qsampler noise
lstm_enc = ConvLSTMCell(n_chan_he, filter_size=[5,5], scale=2, name='encoder') # encoder Op
lstm_dec = ConvLSTMCell(n_chan_hd, filter_size=[5,5], name='decoder') # decoder Op

def linear(x,output_dim):
    """
    affine transformation Wx+b
    assumes x.shape = (batch_size, num_features)
    """
    w=tf.get_variable("w", [x.get_shape()[1], output_dim]) 
    b=tf.get_variable("b", [output_dim], initializer=tf.constant_initializer(0.0))
    return tf.matmul(x,w)+b

def conv(x, n_filters, filter_dim=5, scale=1):
    """
    convolutional transformation W**x+b
    assumes x.shape = (batch_size, A, B, num_features)
    """
    n_batch, n_dim, _, n_input_chan = x.get_shape()
    
    W = tf.get_variable('W', shape=[filter_dim, filter_dim, n_input_chan, n_filters],
                          initializer=tf.random_normal_initializer(stddev=1e-3))
    b = tf.get_variable('b', [n_filters], initializer=tf.constant_initializer(0.))

    # create and track pre-activations
    x = tf.nn.conv2d(x, W, strides=[1, scale, scale, 1], padding='SAME')
    x = tf.nn.bias_add(x, b)

    return x    

def encode(state,input):
    """
    run LSTM
    state = previous encoder state
    input = cat(read,h_dec_prev)
    returns: (output, new_state)
    """
    with tf.variable_scope("encoder",reuse=DO_SHARE):
        return lstm_enc(input,state)

def sampleQ(h_enc):
    """
    Samples Zt ~ normrnd(mu,sigma) via reparameterization trick for normal dist
    mu is (batch,z_size)
    """
    with tf.variable_scope("mu",reuse=DO_SHARE):
        mu = conv(h_enc,n_chan_z)
    with tf.variable_scope("sigma",reuse=DO_SHARE):
        logsigma = conv(h_enc,n_chan_z)
        sigma = tf.exp(logsigma)
    return (mu + sigma * e, mu, logsigma, sigma)

def decode(state,input):
    with tf.variable_scope("decoder",reuse=DO_SHARE):
        return lstm_dec(input, state)

def read(r, n_chan=n_chan_r):
    with tf.variable_scope("read",reuse=DO_SHARE):
        r_down = conv(r,n_chan,filter_dim=3,scale=2)
        return r_down

def write(h_dec, n_chan=n_chan_r):
    with tf.variable_scope("write",reuse=DO_SHARE):
        h_conv = conv(h_dec,n_chan*4)
        return tf.depth_to_space(h_conv, 2)

## STATE VARIABLES ## 

rs=[0]*T # sequence of canvases
mus,logsigmas,sigmas=[0]*T,[0]*T,[0]*T # gaussian params generated by SampleQ. We will need these for computing loss.

# initial states
h_dec_prev=tf.zeros((batch_size,A/2,B/2,n_chan_hd))
h_enc_prev=tf.zeros((batch_size,A/2,B/2,n_chan_he))
enc_state=lstm_enc.zero_state(batch_size, A/2, B/2)
dec_state=lstm_dec.zero_state(batch_size, A/2, B/2)

## DRAW MODEL ## 

# construct the unrolled computational graph
for t in range(T):
    r_prev = tf.zeros((batch_size,A,B,n_chan_r)) if t==0 else rs[t-1]
    m, s = tf.split(r_prev, 2, 3)
    epsilon = x - m # error image
    h_enc, enc_state = encode(enc_state,tf.concat([x,epsilon], 3)) # h_enc_prev is in enc_state; TODO: add h_dec
    z, mus[t], logsigmas[t], sigmas[t] = sampleQ(h_enc)
    r_prev_down = read(r_prev)
    h_dec, dec_state = decode(dec_state, tf.concat([z,r_prev_down], 3)) # !
    rs[t] = r_prev+write(h_dec) # store results # !
    h_dec_prev = h_dec
    h_enc_prev = h_enc
    DO_SHARE=True # from now on, share variables

## LOSS FUNCTION ## 
def log_normal2(x, mean, log_var, eps=1e-5):
    import math
    c = - 0.5 * math.log(2*math.pi)
    return c - log_var/2 - (x - mean)**2 / (2 * tf.exp(log_var) + eps) 

# reconstruction term appears to have been collapsed down to a single scalar value (rather than one per item in minibatch)
m, s = tf.split(rs[-1], 2, 3)

# after computing binary cross entropy, sum across features then take the mean of those sums across minibatches
Lx=tf.reduce_sum(-log_normal2(x, m, s),[1,2,3]) # reconstruction term
Lx=tf.reduce_mean(Lx,0)

kl_terms=[0]*T
for t in range(T):
    mu2=tf.square(mus[t])
    sigma2=tf.square(sigmas[t])
    logsigma=logsigmas[t]
    kl_terms[t]=0.5*tf.reduce_sum(mu2+sigma2-2*logsigma,1)-.5 # each kl term is (1xminibatch)
KL=tf.add_n(kl_terms) # this is 1xminibatch, corresponding to summing kl_terms from 1:T
Lz=tf.reduce_mean(KL) # average over minibatches

cost=Lx+Lz

## OPTIMIZER ## 

optimizer=tf.train.AdamOptimizer(learning_rate)
grads=optimizer.compute_gradients(cost)
for i,(g,v) in enumerate(grads):
    if g is not None:
        grads[i]=(tf.clip_by_norm(g,5),v) # clip gradients
    else:
        print 'WARNING NO GRAD FOR VAR:', v
train_op=optimizer.apply_gradients(grads)

## RUN TRAINING ## 
import data
Xtr, Ytr, Xte, Yte = data.load_cifar10()
Xtr /= 256
Xte /= 256
train_data = data.Dataset(Xtr, Ytr)

fetches=[]
fetches.extend([Lx,Lz,m,train_op])
Lxs=[0]*train_iters
Lzs=[0]*train_iters

sess=tf.InteractiveSession()

saver = tf.train.Saver() # saves variables learned during training
tf.global_variables_initializer().run()
#saver.restore(sess, "/tmp/draw/drawmodel.ckpt") # to restore from model, uncomment this line

from plot_data import xrecons_color_grid
import matplotlib
matplotlib.use('Agg') # Force matplotlib to not use any Xwindows backend.
import matplotlib.pyplot as plt

for i in range(train_iters):
    xtrain,_=train_data.next_batch(batch_size) # xtrain is (batch_size x A x B x chan)
    feed_dict={x:xtrain}
    results=sess.run(fetches,feed_dict)
    Lxs[i],Lzs[i],m_out,_=results
    if i%100==0:
        print("iter=%d : Lx: %f Lz: %f" % (i,Lxs[i],Lzs[i]))
        m_out = m_out.reshape(batch_size, A*B, 3)[:25]
        m_out /= m_out.max()
        img = xrecons_color_grid(m_out[:25], B, A)
        # xtrain = xtrain[:25].reshape(25, A*B, 3)
        # xtrain /= 256
        # img = xrecons_color_grid(xtrain, B, A)
        plt.imshow(img)
        plt.savefig('reconstructions.cifar10.%d.png' % i)

## TRAINING FINISHED ## 

canvases=sess.run(rs,feed_dict) # generate some examples
canvases=np.array(canvases) # T x batch x img_size

out_file=os.path.join(FLAGS.data_dir,"draw_data.npy")
np.save(out_file,[canvases,Lxs,Lzs])
print("Outputs saved in file: %s" % out_file)

ckpt_file=os.path.join(FLAGS.data_dir,"drawmodel.ckpt")
print("Model saved in file: %s" % saver.save(sess,ckpt_file))

sess.close()
