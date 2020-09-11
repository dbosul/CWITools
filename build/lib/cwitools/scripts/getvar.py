"""Estimate 3D variance based on an input data cube."""

#Standard Imports
import argparse
import os

#Third-party Imports
from astropy.io import fits

#Local Imports
from cwitools.reduction import estimate_variance
from cwitools import utils
import cwitools

def parser_init():
    """Create command-line argument parser for this script."""
    parser = argparse.ArgumentParser(
        description="""Estimate 3D variance based on an input data cube.""")
    parser.add_argument(
        'cube',
        type=str,
        metavar='path',
        help='Input cube whose 3D variance you would like to estimate.'
    )
    parser.add_argument(
        '-window',
        type=int,
        help='Size of wavelength bin, in Angstrom, for 2D layer variance estimate.',
        default=50
    )
    parser.add_argument(
        '-wmask',
        type=str,
        nargs='+',
        metavar='<w0:w1 w2:w3 ...>',
        help='Wavelength range(s) in the form (A:B) to mask when fitting.'
    )
    parser.add_argument(
        '-mask_neb',
        metavar='<redshift>',
        type=float,
        help='Prove redshift to auto-mask nebular emission.'
    )
    parser.add_argument(
        '-vwidth',
        metavar='<km/s>',
        type=float,
        help='Velocity width (km/s) around nebular lines to mask, if using -mask_neb.',
        default=500
    )
    parser.add_argument(
        '-out',
        type=str,
        metavar='str',
        help='Filename for output. Default is input + .var.fits'
    )
    parser.add_argument(
        '-log',
        metavar="<log_file>",
        type=str,
        help="Log file to save output in."
    )
    parser.add_argument(
        '-silent',
        help="Set flag to suppress standard terminal output.",
        action='store_true'
    )
    return parser

def main(cube, window=50, wmask=None, mask_neb_z=None, mask_neb_dv=500, out=None,
         log=None, silent=True):
    """Estimate 3D variance based on an input data cube."""

    #Set global parameters
    cwitools.silent_mode = silent
    cwitools.log_file = log

    utils.output_func_summary("GET_VAR", locals())

    #Try to load the fits file
    if os.path.isfile(cube):
        data_fits = fits.open(cube)
    else:
        raise FileNotFoundError("Input file not found.")

    if mask_neb_z is not None:
        wmask += utils.get_nebmask(
            data_fits[0].header,
            z=mask_neb_z,
            vel_window=mask_neb_dv,
            mode='tuples'
        )

    vardata = estimate_variance(
        data_fits,
        window=window,
        wmasks=wmask
    )

    if out is None:
        out = cube.replace('.fits', '.var.fits')

    var_fits = fits.HDUList([fits.PrimaryHDU(vardata)])

    var_fits[0].header = data_fits[0].header
    var_fits.writeto(out, overwrite=True)
    utils.output("\tSaved %s\n" % out)

#Call using dict and argument parser if run from command-line
if __name__ == "__main__":

    arg_parser = parser_init()
    args = arg_parser.parse_args()

    #Parse wmask argument properly into list of float-tuples
    if isinstance(args.wmask, list):
        try:
            for i, wpair in enumerate(args.wmask):
                args.wmask[i] = tuple(float(x) for x in wpair.split(':'))
        except:
            raise ValueError("Could not parse wmask argument (%s)." % args.wmask)

    main(**vars(args))
