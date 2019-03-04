import numpy as np
from keras import layers, models, optimizers
from keras import backend as K
from keras.layers import concatenate, Permute, Lambda
from keras.utils import to_categorical
from keras import regularizers
from PIL import Image
import random
import scipy
from capslayer import *

K.set_image_data_format('channels_last')

def CapsNet(input_shape, n_class, routings, lanes = 4, lanesize = 1, depth = 1):
    x = layers.Input(shape=input_shape)

    convs = [];
    for i in range(0,lanes):
      output = layers.Conv2D(filters=lanesize*16, kernel_size=1, strides=1, padding='same', name='convs1-'+str(i))(x)
      output = layers.SeparableConv2D(filters=lanesize*16, kernel_size=6, strides=1, padding='valid', activation='relu', name='convs'+str(i))(output)
      for j in range(3):
        output = layers.Conv2D(filters=lanesize*16, kernel_size=3, strides=1, padding='same', activation='relu')(output)
      convs = convs + [output]

    primarycaps = [];
    for i in range(0,lanes):
        primarycaps = primarycaps + [PrimaryCap(convs[i], dim_capsule=16, n_channels=lanesize*2, kernel_size=6, strides=2, padding='valid', i = i)]
    
    digitcapsl = [];
    for i in range(0,lanes):
        digitcapsl = digitcapsl + [CapsuleLayer(num_capsule=1, dim_capsule=n_class, routings=routings, batchsize = args.batch_size, name='digitcaps'+str(i))(primarycaps[i])]

    digitcaps1 = Lambda(lambda ls : K.permute_dimensions(concatenate(ls, axis=1), [0,2,1]))(digitcapsl)

    digitcaps =  layers.Dropout(args.dropoutrate, (1, lanes))(digitcaps1)

    out_caps = Length(name='capsnet')(digitcaps)

    # Decoder network.
    y = layers.Input(shape=(n_class,))
    masked_by_y = Mask()([digitcaps1, y])  # The true label is used to mask the output of capsule layer. For training
    masked = Mask()(digitcaps1)  # Mask using the capsule with maximal length. For prediction

    # Shared Decoder model in training and prediction
    decoder = models.Sequential(name='decoder')
    decoder.add(layers.Dense(512, activation='relu', input_dim=lanes*n_class))
    decoder.add(layers.Dense(1024, activation='relu'))
    decoder.add(layers.Dense(np.prod(input_shape), activation='sigmoid'))
    decoder.add(layers.Reshape(target_shape=input_shape, name='out_recon'))

    # Models for training and evaluation (prediction)
    train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
    eval_model = models.Model(x, [out_caps, decoder(masked)])

    # manipulate model
    noise = layers.Input(shape=(n_class, lanes))
    noised_digitcaps = layers.Add()([digitcaps1, noise])
    masked_noised_y = Mask()([noised_digitcaps, y])
    manipulate_model = models.Model([x, y, noise], decoder(masked_noised_y))
    return train_model, eval_model, manipulate_model

  
def margin_loss(y_true, y_pred):
    """
    Margin loss for Eq.(4). When y_true[i, :] contains not just one `1`, this loss should work too. Not test it.
    :param y_true: [None, n_classes]
    :param y_pred: [None, num_capsule]
    :return: a scalar loss value.
    """
    L = y_true * K.square(K.maximum(0., 0.9 - y_pred)) + \
        0.5 * (1 - y_true) * K.square(K.maximum(0., y_pred - 0.1))

    return K.mean(K.sum(L, 1))


def train(model, data, args):
    """
    Training a CapsuleNet
    :param model: the CapsuleNet model
    :param data: a tuple containing training and testing data, like `((x_train, y_train), (x_test, y_test))`
    :param args: arguments
    :return: The trained model
    """
    # unpacking the data
    (x_train, y_train), (x_test, y_test) = data

    # callbacks
    log = callbacks.CSVLogger(args.save_dir + '/log.csv')
    tb = callbacks.TensorBoard(log_dir=args.save_dir + '/tensorboard-logs',
                               batch_size=args.batch_size, histogram_freq=int(args.debug))
    checkpoint = callbacks.ModelCheckpoint(args.save_dir + 'weights-{epoch:02d}.h5', monitor='val_capsnet_acc',
                                           save_best_only=True, save_weights_only=True, verbose=1)
    lr_decay = callbacks.LearningRateScheduler(schedule=lambda epoch: args.lr * (args.lr_decay ** epoch))

    model.compile(optimizer=optimizers.Adam(lr=args.lr),
            loss=[margin_loss, 'mse'],
            loss_weights=[1., args.lam_recon],
            metrics={'capsnet': 'accuracy'})

    
    # Training without data augmentation:
    model.fit([x_train, y_train], [y_train, x_train], batch_size=args.batch_size, epochs=args.epochs,
              validation_data=[[x_test, y_test], [y_test, x_test]], callbacks=[log, tb, checkpoint, lr_decay])
    """

    # Begin: Training with data augmentation ---------------------------------------------------------------------#
    def train_generator(x, y, batch_size, shift_fraction=0.):
        train_datagen = ImageDataGenerator(width_shift_range=shift_fraction,
                                           height_shift_range=shift_fraction)  # shift up to 2 pixel for MNIST
        generator = train_datagen.flow(x, y, batch_size=batch_size)
        while 1:
            x_batch, y_batch = generator.next()
            yield ([x_batch, y_batch], [y_batch, x_batch])

    # Training with data augmentation. If shift_fraction=0., also no augmentation.
    model.fit_generator(generator=train_generator(x_train, y_train, args.batch_size, args.shift_fraction),
                        steps_per_epoch=int(y_train.shape[0] / args.batch_size),
                        epochs=args.epochs,
                        validation_data=[[x_test, y_test], [y_test, x_test]],
                        callbacks=[log, tb, checkpoint, lr_decay])
    # End: Training with data augmentation -----------------------------------------------------------------------#
    """

    model.save_weights(args.save_dir + '/trained_model.h5')
    print('Trained model saved to \'%s/trained_model.h5\'' % args.save_dir)

    return model


def test(model, data, args):
    x_test, y_test = data
    y_pred, x_recon = model.predict(x_test, batch_size=100)
    print('-'*30 + 'Begin: test' + '-'*30)
    print('Test acc:', np.sum(np.argmax(y_pred, 1) == np.argmax(y_test, 1))/y_test.shape[0])

    img = combine_images(np.concatenate([x_test[:50],x_recon[:50]]))
    image = img * 255
    Image.fromarray(image.astype(np.uint8)).save(args.save_dir + "/real_and_recon.png")
    print()
    print('Reconstructed images are saved to %s/real_and_recon.png' % args.save_dir)
    print('-' * 30 + 'End: test' + '-' * 30)
    plt.imshow(plt.imread(args.save_dir + "/real_and_recon.png"))
    plt.show()


def manipulate_latent(model, data, args):
    print('-'*30 + 'Begin: manipulate' + '-'*30)
    x_test, y_test = data
    index = np.argmax(y_test, 1) == args.digit
    number = np.random.randint(low=0, high=sum(index) - 1)
    x, y = x_test[index][number], y_test[index][number]
    x, y = np.expand_dims(x, 0), np.expand_dims(y, 0)
    noise = np.zeros([1, 10, 16])
    x_recons = []
    for dim in range(16):
        for r in [-0.25, -0.2, -0.15, -0.1, -0.05, 0, 0.05, 0.1, 0.15, 0.2, 0.25]:
            tmp = np.copy(noise)
            tmp[:,:,dim] = r
            x_recon = model.predict([x, y, tmp])
            x_recons.append(x_recon)

    x_recons = np.concatenate(x_recons)

    img = combine_images(x_recons, height=16)
    image = img*255
    Image.fromarray(image.astype(np.uint8)).save(args.save_dir + '/manipulate-%d.png' % args.digit)
    print('manipulated result saved to %s/manipulate-%d.png' % (args.save_dir, args.digit))
    print('-' * 30 + 'End: manipulate' + '-' * 30)


def load_cifar():
    # the data, shuffled and split between train and test sets
    #from keras.datasets import fashion_mnist
    #(x_train, y_train), (x_test, y_test) = fashion_mnist.load_data()
    from keras.datasets import cifar10
    (x_train, y_train), (x_test, y_test) = cifar10.load_data()

    x_train = x_train.reshape(-1, 32, 32, 3).astype('float32') / 255.
    x_test = x_test.reshape(-1, 32, 32, 3).astype('float32') / 255.
    y_train = to_categorical(y_train.astype('float32'))
    y_test = to_categorical(y_test.astype('float32'))
    return (x_train, y_train), (x_test, y_test)

def load_mnist():
    # the data, shuffled and split between train and test sets
    from keras.datasets import fashion_mnist
    (x_train, y_train), (x_test, y_test) = fashion_mnist.load_data()

    x_train = x_train.reshape(-1, 28, 28, 1).astype('float32') / 255.
    x_test = x_test.reshape(-1, 28, 28, 1).astype('float32') / 255.
    y_train = to_categorical(y_train.astype('float32'))
    y_test = to_categorical(y_test.astype('float32'))
    return (x_train, y_train), (x_test, y_test)


import os
import argparse
from keras.preprocessing.image import ImageDataGenerator
from keras import callbacks

# setting the hyper parameters

class A:
  pass
  
for l in [4]: #[2, 4, 8, 16, 32]:
    for s in [1]: #[1,2,4]:
        args = A()
        args.digit = 5
        args.epochs = 20
        args.batch_size = 100
        args.lr = 0.01
        args.lr_decay = 0.9
        args.lam_recon = 0.392
        args.routings = 3
        args.shift_fraction = 0.0
        args.save_dir = "./mnist-result-l"+str(l)+"-s"+str(s)+"/"
        args.debug = False
        args.dropoutrate = 0.0
        regularizers.l1_l2(l1=0.008, l2=0.008)

        # load data
        (x_train, y_train), (x_test, y_test) = load_mnist()

        # define model
        model, eval_model, manipulate_model = CapsNet(input_shape=x_train.shape[1:],
                                                      n_class=len(np.unique(np.argmax(y_train, 1))),
                                                      routings=args.routings,
                                                      lanes = l,
                                                      lanesize = s,
                                                      depth = l)
        model.summary()

        train(model=model, data=((x_train, y_train), (x_test, y_test)), args=args)
