"""Fault fingerprints: where in the spectrum the model fails to reconstruct.

A window's embedding is its per-mel-band squared residual, log-scaled, then
standardised against *that machine's own* normal residuals and L2-normalised.
Standardising per machine removes each unit's quirks, so a fingerprint from
one factory's pump can be compared with another factory's pump.
64 floats per fingerprint; no audio leaves the machine.
"""
import numpy as np


def band_profile(x, x_hat):
    """(n, mels, frames[,1]) pair -> (n, mels) log mean squared residual."""
    r = (x - x_hat).reshape(x.shape[0], x.shape[1], -1)
    return np.log(np.mean(r ** 2, axis=2) + 1e-8)


def normal_stats(profiles):
    return profiles.mean(axis=0), profiles.std(axis=0) + 1e-6


def embed(profiles, mu, sd):
    z = (profiles - mu) / sd
    return z / (np.linalg.norm(z, axis=1, keepdims=True) + 1e-9)


def fingerprint(embeddings):
    """Many window embeddings of one confirmed fault -> one unit vector."""
    v = np.asarray(embeddings).mean(axis=0)
    return v / (np.linalg.norm(v) + 1e-9)


def cosine(v, refs):
    """v: (d,) ; refs: (k, d) of unit vectors -> (k,) similarities."""
    v = np.asarray(v, dtype=float)
    refs = np.atleast_2d(np.asarray(refs, dtype=float))
    v = v / (np.linalg.norm(v) + 1e-9)
    refs = refs / (np.linalg.norm(refs, axis=1, keepdims=True) + 1e-9)
    return refs @ v
