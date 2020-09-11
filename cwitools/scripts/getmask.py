"""Create a binary mask using a DS9 region file."""

#Standard Imports
import argparse
import os

#Third-party Imports
from astropy.io import fits

#Local Imports
import cwitools
from cwitools import extraction, utils

def parser_init():
    """Create command-line argument parser for this script."""
    parser = argparse.ArgumentParser(
        description="Create a binary mask based on a DS9 region file."
        )
    parser.add_argument(
        'reg',
        type=str,
        help='DS9 region file to convert into a mask.'
        )
    parser.add_argument(
        'data',
        type=str,
        help='Data cube or image to create mask for.'
        )
    parser.add_argument(
        '-out',
        type=str,
        help='Output filename. By default will be input data + .mask.fits',
        default=None
        )
    parser.add_argument(
        '-log',
        metavar="<log_file>",
        type=str,
        help="Log file to save output in.",
        default=None
        )
    parser.add_argument(
        '-silent',
        help="Set flag to suppress standard terminal output.",
        action='store_true'
        )
    return parser

def main(reg, data, out=None, log=None, silent=True):
    """Create a binary mask based on a DS9 region file"""

    #Set global parameters
    cwitools.silent_mode = silent
    cwitools.log_file = log

    utils.output_func_summary("GET_MASK", locals())

    if os.path.isfile(data):
        data_fits = fits.open(data)
    else:
        raise FileNotFoundError(data)

    #Get mask
    mask_fits = extraction.reg2mask(data_fits, reg)

    if out is None:
        outfilename = data.replace('.fits', '.mask.fits')
    else:
        outfilename = out

    mask_fits.writeto(outfilename, overwrite=True)
    utils.output("\tSaved %s\n" % outfilename)


#Call using dict and argument parser if run from command-line
if __name__ == "__main__":

    arg_parser = parser_init()
    args = arg_parser.parse_args()

    main(**vars(args))
