"""Fetch the NASA IMS bearing dataset and keep test 2 (bearing 1 failed on the outer race).

    python -m data.download_ims        -> data/raw/ims/ (git-ignored; never committed)

A thin entry point over data/real.py's IMS step: downloads the 1.08 GB archive from the NASA / PHM data
repository, unpacks zip -> 7z -> rar, keeps the bearings Dhadkan uses (test 2: all four, bearing 1 is the
outer-race failure), and deletes the archive. Then `python -m edge.ims` scores it in time order and
writes the replay (edge/models/ims_replay.json, ims_demo_pack.npz) that Demo mode plays as Loom 4, Shed A.
"""

from .real import ims

if __name__ == "__main__":
    ims()
