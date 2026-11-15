"""Small convolutional autoencoder over one 1 s log-mel window (64 x 32)."""

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers

from .config import FRAMES, N_MELS


def build(bottleneck: int = 32) -> keras.Model:
    inp = keras.Input((N_MELS, FRAMES, 1))
    x = inp
    for ch in (16, 32, 64):  # 64x32 -> 8x4
        x = layers.Conv2D(ch, 3, strides=2, padding="same", activation="relu")(x)
    x = layers.Flatten()(x)
    z = layers.Dense(bottleneck, activation="relu", name="code")(x)
    x = layers.Dense(8 * 4 * 64, activation="relu")(z)
    x = layers.Reshape((8, 4, 64))(x)
    for ch in (64, 32, 16):  # 8x4 -> 64x32
        x = layers.Conv2DTranspose(ch, 3, strides=2, padding="same", activation="relu")(x)
    out = layers.Conv2D(1, 3, padding="same")(x)
    model = keras.Model(inp, out, name="dhadkan_ae")
    model.compile(optimizer=keras.optimizers.Adam(1e-3), loss="mse")
    return model


def to_tflite(model: keras.Model) -> bytes:
    conv = tf.lite.TFLiteConverter.from_keras_model(model)
    conv.optimizations = [tf.lite.Optimize.DEFAULT]  # dynamic-range int8 weights
    return conv.convert()
