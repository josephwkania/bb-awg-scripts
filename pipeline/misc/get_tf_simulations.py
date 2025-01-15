import healpy as hp
import numpy as np
from pixell import enmap
from pixell import uharm
from pixell import curvedsky
import os


def _check_pix_type(pix_type):
    """
    ** From SOOPERCOOL/soopercool/map_utils.py **
    Error handling for pixellization types.

    Parameters
    ----------
    pix_type : str
        Pixellization type.
    """
    if not (pix_type in ['hp', 'car']):
        raise ValueError(f"Unknown pixelisation type {pix_type}.")


def lmax_from_map(map, pix_type="hp"):
    """
    ** From SOOPERCOOL/soopercool/map_utils.py **
    Determine the maximum multipole from a map and its
    pixellization type.

    Parameters
    ----------
    map : np.ndarray or enmap.ndmap
        Input map.
    pix_type : str, optional
        Pixellization type.

    Returns
    -------
    int
        Maximum multipole.
    """
    _check_pix_type(pix_type)
    if pix_type == "hp":
        nside = hp.npix2nside(map.shape[-1])
        return 3 * nside - 1
    else:
        _, wcs = map.geometry
        res = np.deg2rad(np.min(np.abs(wcs.wcs.cdelt)))
        lmax = uharm.res2lmax(res)
        return lmax


def read_map(map_file,
             pix_type='hp',
             fields_hp=None,
             convert_K_to_muK=False,
             geometry=None):
    """
    ** From SOOPERCOOL/soopercool/map_utils.py **
    Read a map from a file, regardless of the pixellization type.

    Parameters
    ----------
    map_file : str
        Map file name.
    pix_type : str, optional
        Pixellization type.
    fields_hp : tuple, optional
        Fields to read from a HEALPix map.
    convert_K_to_muK : bool, optional
        Convert K to muK.
    geometry : enmap.geometry, optional
        Enmap geometry.

    Returns
    -------
    map_out : np.ndarray
        Loaded map.
    """
    conv = 1
    if convert_K_to_muK:
        conv = 1.e6
    _check_pix_type(pix_type)
    if pix_type == 'hp':
        kwargs = {"field": fields_hp} if fields_hp is not None else {}
        m = hp.read_map(map_file, **kwargs)
    else:
        m = enmap.read_map(map_file, geometry=geometry)

    return conv*m


def main(args):
    """
    """
    pix_type = args.pix_type
    n_sims = args.n_sims
    smooth_fwhm = args.smooth_fwhm
    nside = args.nside

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    if pix_type == "car":
        template_map = read_map(args.car_template_map, pix_type="car")
        shape, wcs = template_map.geometry
        new_shape = (3,) + shape[-2:]
        template = enmap.zeros(new_shape, wcs)
        lmax = lmax_from_map(template, pix_type="car")
    else:
        lmax = 3 * nside - 1

    lmax_sim = lmax + 500

    ells = np.arange(lmax_sim + 1)

    ps = 1 / (ells + 10) ** 2

    for id_sim in range(n_sims):

        alms = hp.synalm(ps, lmax=lmax_sim)

        for i, tag in enumerate(["pureT", "pureE", "pureB"]):
            alms_list = np.zeros((3, *alms.shape), dtype=np.complex128)
            alms_list[i, :] += alms

            if pix_type == "hp":
                map = hp.alm2map(
                    alms_list, nside=nside, lmax=lmax_sim,
                    fwhm=np.deg2rad(smooth_fwhm/60)
                )
                hp.write_map(
                    f"{out_dir}/{tag}_nside{nside}_fwhm{smooth_fwhm}_sim{id_sim:04d}.fits",  # noqa
                    map,
                    overwrite=True
                )
            elif pix_type == "car":
                map = curvedsky.alm2map(
                    alms_list,
                    template
                )
                enmap.write_map(
                    f"{out_dir}/{tag}_fwhm{smooth_fwhm}_sim{id_sim:04d}_CAR.fits",  # noqa
                    map
                )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pix_type",
        help="Pixelization type, either 'hp' or 'car'."
    )
    parser.add_argument(
        "--car_template_map",
        help="Example map that has the same format and resolution as the sims",
        default=None
    )
    parser.add_argument(
        "--nside",
        type=int,
        help="Healpix nside"
    )
    parser.add_argument(
        "--smooth_fwhm",
        type=float,
        help="Smoothing scale FWHM in arcmin"
    )
    parser.add_argument(
        "--n_sims",
        type=int,
        help="Number of simulations"
    )
    parser.add_argument(
        "--out_dir",
        help="Output directory"
    )

    args = parser.parse_args()

    main(args)
