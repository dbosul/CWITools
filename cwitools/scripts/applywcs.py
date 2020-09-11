"""Apply WCS: Update FITS Headers using a WCS correction table"""

#Standard Imports
import argparse
import warnings
import sys

#Third-party Imports
import numpy as np
from astropy.io import fits

#Local Imports
from cwitools import utils, config

def parser_init():
    """Create command-line argument parser for this script."""
    parser = argparse.ArgumentParser(description='Apply a WCS corrections file to data.')
    parser.add_argument(
        'wcs_table',
        type=str,
        help='WCS correction file (see cwi_measurewcs.py)',
        )
    parser.add_argument(
        'ctypes',
        metavar="cube_type(s)",
        type=str,
        nargs='+',
        help='Type(s) of file to apply to. Use spaces to separate multiple values.',
        )
    parser.add_argument(
        '-ext',
        metavar="<file_ext>",
        type=str,
        help='File extension for corrected files (Def: .wc.fits)',
        default=".wc.fits"
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

def main(wcs_table, ctypes="icubes.fits", ext=".wc.fits", log=None, silent=None):
    """Apply a WCS corrections table to a set of FITS images.

    Args:
        wcs_table (str): The path to the WCS correction table file (.wcs)
        ctypes (list or str): The file type to apply corrections to. For example,
            'icubes.fits'. A list of strings can be provided to apply the WCS
            correction to multuple cubetypes, e.g. ['icubes.fits', 'ocubes.fits']
        ext (str): The file extension for the updated files, such as 'icubes.fits'
        log (str): The path to a log file to save output to (default: None)
        silent (bool): Set to FALSE to turn on standard terminal output.
        arg_parser (argparse.ArgumentParser): For internal use only. Enables
            function to be called from the command line. Arguments passed via
            an ArgumentParser override others.

    Returns:
        None
    """

    config.set_temp_output_mode(log, silent)
    utils.output_func_summary("APPLY_WCS", locals())

    utils.output("\n\tCorrecting WCS Axes based on %s\n" % wcs_table)

    #Ensure ctypes is a list, not a string
    if isinstance(ctypes, str):
        ctypes = [ctypes]

    #Open table file
    try:
        wcs_table = open(wcs_table)
    except FileNotFoundError:
        utils.output("\tCould not find WCS correction file: %s\n" % wcs_table)
        sys.exit()

    #Create basic data structures
    ids = []
    cr_matrix = []
    in_dir = "."
    search_depth = 3

    #Parse data from file
    for i, line in enumerate(wcs_table):

        line = line.replace("\n", "")

        if "INPUT_DIRECTORY" in line:
            in_dir = line.split("=")[1].replace(" ", "")

        elif "SEARCH_DEPTH" in line:
            search_depth = int(line.split("=")[1])

        elif line[0] == ">":
            vals = line[1:].split()
            ids.append(vals[0])
            cr_cols = [float(x) for x in vals[1:]]
            cr_matrix.append(cr_cols)

        else:
            continue
    cr_matrix = np.array(cr_matrix)

    #Output correction table header
    utils.output("\n\t%40s %10s %10s %10s\n" % ("Filename", "Ax1Cor?", "Ax2Cor?", "Ax3Cor?"))

    #Loop over file types
    for ctype in ctypes:

        #Load and loop over individual FITS files
        input_files = utils.find_files(ids, in_dir, ctype, depth=search_depth)
        for i, filename in enumerate(input_files):
            in_fits = fits.open(filename)
            ax1, ax2, ax3 = "No", "No", "No"

            if 0 <= cr_matrix[i, 0] <= 360:
                in_fits[0].header["CRVAL1"] = cr_matrix[i, 0]
                in_fits[0].header["CRPIX1"] = cr_matrix[i, 3]
                ax1 = "Yes"

            else:
                warnings.warn("Invalid RA / CRVAL1. Must be 0-360 deg.")

            if -90 <= cr_matrix[i, 1] <= 90:
                in_fits[0].header["CRVAL2"] = cr_matrix[i, 1]
                in_fits[0].header["CRPIX2"] = cr_matrix[i, 4]
                ax2 = "Yes"

            else:
                warnings.warn("Invalid DEC / CRVAL2. Must be -90 to +90 deg.")

            if cr_matrix[i, 2] > 0:
                in_fits[0].header["CRVAL3"] = cr_matrix[i, 2]
                in_fits[0].header["CRPIX3"] = cr_matrix[i, 5]
                ax3 = "Yes"

            outfilename = filename.replace(".fits", ext)
            in_fits.writeto(outfilename, overwrite=True)
            outfilename_short = outfilename.split("/")[-1]
            utils.output("\t%40s %10s %10s %10s\n" % (outfilename_short, ax1, ax2, ax3))
            
    config.restore_output_mode()

#Call using dict and argument parser if run from command-line
if __name__ == "__main__":

    arg_parser = parser_init()
    args = arg_parser.parse_args()

    main(**vars(args))
