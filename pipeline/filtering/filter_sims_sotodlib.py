import numpy as np
import healpy as hp
import argparse
import logging
import sqlite3
import os
import yaml
import sys
import time
import sotodlib.mapmaking.demod_mapmaker as dmm

from sotodlib.mapmaking.noise_model import NmatUnit
from sotodlib.core import Context
from sotodlib.site_pipeline import preprocess_tod
from sotodlib.core.metadata import loader
from sotodlib.coords import demod
from pixell import enmap, utils
from mpi4py import MPI

sys.path.append("/global/homes/k/kwolz/bbdev/bb-awg-scripts/pipeline/bundling")
sys.path.append("/global/homes/k/kwolz/bbdev/bb-awg-scripts/pipeline/misc")
from coordinator import BundleCoordinator  # noqa
from mpi_utils import distribute_tasks  # noqa


def erik_make_map(obs, shape=None, wcs=None, nside=None, site=None):
    """
    Run the healpix mapmaker (currently in sotodlib sat-mapmaking-er-dev) on a
    given observation.

    Parameters
    ----------
        obs: axis manager
            Input TOD, after demodulation.
        shape: numpy.ndarray
            Shape of the output map geometry
        wcs: wcs
            WCS of the output map geometry
        nside: int
            Nside of the output map
        site: str
            Site of the observation, e.g. `so_sat1`
    Returns
    -------
        wmap: numpy.ndarray
            Output weighted TQU map, in healpix NESTED scheme
        weights: numpy.ndarray
            Output TQU weights, in healpix NESTED scheme
    """
    obs.wrap("weather", np.full(1, "toco"))
    obs.wrap("site", np.full(1, site))
    obs.flags.wrap(
        'glitch_flags',
        (obs.preprocess.turnaround_flags.turnarounds
         + obs.preprocess.jumps_2pi.jump_flag
         + obs.preprocess.glitches.glitch_flags)
    )
    mapmaker = dmm.setup_demod_map(NmatUnit(), shape=shape, wcs=wcs,
                                   nside=nside)
    mapmaker.add_obs('signal', obs)
    wmap = mapmaker.signals[0].rhs[0]
    weights = np.diagonal(mapmaker.signals[0].div[0], axis1=0, axis2=1)
    weights = np.moveaxis(weights, -1, 0)

    return wmap, weights


def get_logger(fmt=None, datefmt=None, debug=False, **kwargs):
    """Return logger from logging module
    code from pspipe

    Parameters
    ----------
        fmt: string
        the format string that preceeds any logging message
        datefmt: string
        the date format string
        debug: bool
        debug flag
    """
    #fmt = fmt or "%(asctime)s - %(filename)s:%(lineno)d - %(levelname)s: %(message)s" # noqa
    fmt = fmt or "%(asctime)s - %(message)s"
    datefmt = datefmt or "%d-%b-%y %H:%M:%S"
    logging.basicConfig(
        format=fmt,
        datefmt=datefmt,
        level=logging.DEBUG if debug else logging.INFO,
        force=True
    )
    return logging.getLogger(kwargs.get("name"))


def main(args):
    """
    """
    if args.pix_type not in ["hp", "car"]:
        raise ValueError(
            "Unknown pixel type, must be 'car' or 'hp'."
        )

    comm = MPI.COMM_WORLD
    size = comm.Get_size()
    rank = comm.Get_rank()

    # ArgumentParser
    out_dir = args.output_directory
    os.makedirs(out_dir, exist_ok=True)

    plot_dir = f"{out_dir}/plots"
    os.makedirs(plot_dir, exist_ok=True)

    atomics_dir = f"{out_dir}/atomics_sims"
    os.makedirs(atomics_dir, exist_ok=True)

    # Databases
    bundle_db = args.bundle_db
    atom_db = args.atomic_db

    # Config files
    preprocess_config = args.preprocess_config

    # Sim related arguments
    map_dir = args.map_dir
    map_string_format = args.map_string_format
    sim_ids = args.sim_ids

    # Pixelization arguments
    pix_type = args.pix_type
    if pix_type == "hp":
        nside = args.nside
        mfmt = ".fits"  # TODO: fits.gz for HEALPix
    elif pix_type == "car":
        car_map_template = args.car_map_template
        nside = None
        mfmt = ".fits"

    if pix_type == "car" and car_map_template is not None:
        w = enmap.read_map(car_map_template)
        # shape = w.shape[-2:]
        wcs = w.wcs

    # Bundle query arguments
    freq_channel = args.freq_channel
    null_prop_val_inter_obs = args.null_prop_val_inter_obs
    bundle_id = args.bundle_id

    # Extract list of ctimes from bundle database for the given
    # bundle_id - null split combination
    if os.path.isfile(bundle_db):
        print(f"Loading from {bundle_db}.")
        bundle_coordinator = BundleCoordinator.from_dbfile(
            bundle_db, bundle_id=bundle_id,
            null_prop_val=null_prop_val_inter_obs
        )
    else:
        raise ValueError(f"DB file does not exist: {bundle_db}")

    ctimes = bundle_coordinator.get_ctimes(
        bundle_id=bundle_id, null_prop_val=null_prop_val_inter_obs
    )

    # Read restrictive list of atomic-map metadata
    # (obs_id, wafer, freq_channel) from file, and intersect it with the
    # metadata in the bundling database.
    atomic_restrict = []
    if args.atomic_list is not None:
        atomic_restrict = list(
            map(tuple, np.load(args.atomic_list)["atomic_list"])
        )

    atomic_metadata = []
    db_con = sqlite3.connect(atom_db)
    db_cur = db_con.cursor()
    for ctime in ctimes:
        res = db_cur.execute(
            "SELECT obs_id, wafer FROM atomic WHERE "
            f"freq_channel == '{freq_channel}' AND ctime == '{ctime}'"
        )
        res = res.fetchall()
        for obs_id, wafer in res:
            atom_id = (obs_id, wafer, freq_channel)
            restrict = (args.atomic_list is not None
                        and (atom_id not in atomic_restrict))
            if not restrict and atom_id not in atomic_metadata:
                atomic_metadata.append((obs_id, wafer, freq_channel))
    db_con.close()

    print(
        f"{len(atomic_metadata)} atomic file names (bundle {bundle_id})"
    )

    # Load preprocessing pipeline and extract from it list of preprocessing
    # metadata (detectors, samples, etc.) corresponding to each atomic map
    config = yaml.safe_load(open(preprocess_config, "r"))
    context = config["context_file"]
    ctx = Context(context)

    # Distribute [natomics x nsims] tasks among [size] workers
    if "," in sim_ids:
        id_min, id_max = sim_ids.split(",")
    else:
        id_min = sim_ids
        id_max = id_min
    id_min = int(id_min)
    id_max = int(id_max)

    ids = np.arange(id_min, id_max+1)
    mpi_shared_list = [(i, j) for i in ids for j in atomic_metadata]

    log = get_logger()
    task_ids = distribute_tasks(size, rank, len(mpi_shared_list), logger=log)
    local_mpi_list = [mpi_shared_list[i] for i in task_ids]

    # Loop over local tasks (sim_id, atomic_id). For each of these, do:
    # * read simulated map
    # * load map into timestreams, apply preprocessing
    # * apply mapmaking
    local_wmaps = []
    local_weights = []
    local_labels = []

    for sim_id, (obs_id, wafer, freq) in local_mpi_list:
        start = time.time()
        map_fname = map_string_format.format(sim_id=sim_id)
        map_file = f"{map_dir}/{map_fname}"

        try:
            sim = enmap.read_map(map_file)
        except ValueError:  # if map is not enmap
            sim = hp.read_map(map_file, field=[0, 1, 2])

        log.info(f"***** Doing {obs_id} {wafer} {freq} "
                 f"and SIMULATION {sim_id} *****")
        dets = {"wafer_slot": wafer, "wafer.bandpass": freq}
        meta = ctx.get_meta(obs_id, dets=dets)

        # Focal plane thinning
        if args.fp_thin is not None:
            fp_thin = int(args.fp_thin)
            thinned = [m for im, m in enumerate(meta.dets.vals)
                       if im % fp_thin == 0]
            meta.restrict("dets", thinned)

        # Missing pointing not cut in preprocessing
        meta.restrict(
            "dets", meta.dets.vals[~np.isnan(meta.focal_plane.gamma)]
        )
        try:
            aman = preprocess_tod.load_preprocess_tod_sim(
                obs_id,
                sim_map=sim,
                configs=config,
                meta=meta,
                modulated=True,
                site="so_sat1",  # new field required from new from_map()
                ordering="RING"  # new field required for healpix
            )
            log.info(f"Loaded {obs_id}, {wafer}, {freq}")
        except loader.LoaderError:
            print(f"ERROR: {obs_id} {wafer} {freq} metadata is not there. "
                  "SKIPPING.")
            continue

        if aman.dets.count <= 1:
            continue

        if pix_type == "car":
            filtered_sim = demod.make_map(
                aman,
                res=10*utils.arcmin,
                wcs_kernel=wcs,
            )
            wmap, w = filtered_sim["weighted_map"], filtered_sim["weight"]
            w = np.moveaxis(w.diagonal(), -1, 0)

        elif pix_type == "hp":
            wmap, w = erik_make_map(aman, nside=nside, site="so_sat1")

        local_wmaps.append(wmap)
        local_weights.append(w)
        local_labels.append(sim_id)

        # Saving filtered atomics to disk
        log.info(f"Rank {rank} saving labels {local_labels}")
        atomic_fname = map_string_format.format(sim_id=sim_id).replace(
            mfmt,
            f"_obsid{obs_id}_{wafer}_{freq_channel}{mfmt}"
        )
        f_wmap = f"{atomics_dir}/{atomic_fname.replace(mfmt, '_wmap' + mfmt)}"
        f_w = f"{atomics_dir}/{atomic_fname.replace(mfmt, '_w' + mfmt)}"

        if pix_type == "car":
            enmap.write_map(f_wmap, wmap)
            enmap.write_map(f_w, w)

        elif pix_type == "hp":
            hp.write_map(
                f_wmap, wmap, dtype=np.float32, overwrite=True, nest=True
            )
            hp.write_map(
                f_w, w, dtype=np.float32, overwrite=True, nest=True
            )
        end = time.time()
        print(f"*** ELAPSED TIME for filtering: {end - start} seconds. ***")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--atomic-db",
        help="Path to the atomic maps database."
    )
    parser.add_argument(
        "--atomic_list",
        help="Npz file with list of atomic maps to restrict the atomic_db to.",
        default=None
    )
    parser.add_argument(
        "--bundle-db",
        help="Path to the bundling database."
    )
    parser.add_argument(
        "--preprocess-config",
        help="Path to the preprocessing config file."
    )
    parser.add_argument(
        "--map-dir",
        help="Directory containing the maps to filter."
    )
    parser.add_argument(
        "--map_string_format",
        help="String formatting; must contain {sim_id}."
    )
    parser.add_argument(
        "--sim-ids",
        help="String of format 'sim_id_min,sim_id_max', or only 'sim_id'."
    )
    parser.add_argument(
        "--output-directory",
        help="Output directory for the filtered maps."
    )
    parser.add_argument(
        "--freq-channel",
        help="Frequency channel to filter."
    )
    parser.add_argument(
        "--bundle-id",
        type=int,
        default=0,
        help="Bundle ID to filter.",
    )
    parser.add_argument(
        "--null_prop_val_inter_obs",
        help="Null property value for inter-obs splits, e.g. 'pwv_low'.",
        default=None
    )
    parser.add_argument(
        "--nside",
        help="Nside parameter for HEALPIX mapmaker.",
        type=int,
        default=512
    )
    parser.add_argument(
        "--pix_type",
        help="Pixelization type; 'hp' or 'car",
        default='hp'
    )
    parser.add_argument(
        "--car_map_template",
        help="path to CAR coadded (hits) map to be used as geometry template",
        default=None
    )
    parser.add_argument(
        "--fp-thin",
        help="Focal plane thinning factor",
        default=None
    )

    args = parser.parse_args()
    main(args)
