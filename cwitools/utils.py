"""Generic tools for saving files, etc."""
from astropy.io import fits
import cwitools
import os
import sys
import warnings

clist_template = {
    "INPUT_DIRECTORY":"./",
    "SEARCH_DEPTH":3,
    "OUTPUT_DIRECTORY":"./",
    "ID_LIST":[]
}

def get_fits(data, header=None):
    hdu = fits.PrimaryHDU(data, header=header)
    hdulist = fits.HDUList([hdu])
    return hdulist

def set_cmdlog(path):
    cwitools.command_log = path

def log_command(sys_argv, logfile=None):
    """Append a terminal command reflected by sys.argv to the log file

    Args:
        sys_argv: The sys.argv attribute from the script that was called.

    Returns:
        int: 1 if successfully written to file, -1 if failed.
    """
    cmd_string = " ".join(sys.argv)

    #If no log-file given, warn user and return bad flag
    if logfile == None:
        warnings.warn("Command will not be saved. No log file given.")
        return -1

    #Let user know if new file is being made (can help avoid typos)
    if not(os.path.isfile(logfile)):
        warnings.warn("%s does not exist. File will be created." % logfile)

    try:
        cmd_log = open(logfile, 'a')
        cmd_log.write(cmd_string + '\n')
        cmd_log.close()

    except:
        raise ValueError("Error opening/writing to log file: %s" % logfile)

    return 1

def find_files(id_list, datadir, cubetype, depth=3):
    """Finds the input files given a CWITools parameter file and cube type.

    Args:
        params (dict): CWITools parameters dictionary.
        cubetype (str): Type of cube (e.g. icubes.fits) to load.

    Returns:
        list(string): List of file paths of input cubes.

    Raises:
        NotADirectoryError: If the input directory does not exist.

    """

    #Check data directory exists
    if not os.path.isdir(datadir):
        raise NotADirectoryError("Data directory (%s) does not exist. Please correct and try again." % datadir)

    #Load target cubes
    N_files = len(id_list)
    target_files = []
    typeLen = len(cubetype)

    for root, dirs, files in os.walk(datadir):

        if root[-1] != '/': root += '/'
        rec = root.replace(datadir, '').count("/")

        if rec > depth: continue
        else:
            for f in files:
                if f[-typeLen:] == cubetype:
                    for i,ID in enumerate(id_list):
                        if ID in f:
                            target_files.append(root + f)

    #Print file paths or file not found errors
    if len(target_files) < len(id_list):
        warnings.warn("Some files were not found:")
        for id in id_list:
            is_in = np.array([ id in x for x in target_files])
            if not np.any(is_in):
                warnings.warn("Image with ID %s and type %s not found." % (id, cubetype))


    return sorted(target_files)

def parse_cubelist(filepath):
    """Load a CWITools parameter file into a dictionary structure.

    Args:
        path (str): Path to CWITools .list file

    Returns:
        dict: Python dictionary containing the relevant fields and information.

    """
    global clist_template
    clist = {k:v for k, v in clist_template.items()}

    #Parse file
    listfile = open(filepath, 'r')
    for line in listfile:

        line = line[:-1] #Trim new-line character
        #Skip empty lines
        if line == "":
            continue

        #Add IDs when indicated by >
        elif line[0] == '>':
            clist["ID_LIST"].append(line.replace('>', ''))

        elif '=' in line:

            line = line.replace(' ', '')     #Remove white spaces
            line = line.replace('\n', '')    #Remove line ending
            line = line.split('#')[0]        #Remove any comments
            key, val = line.split('=') #Split into key, value pair
            if key.upper() in clist:
                clist[key] = val
            else:
                raise ValuError("Unrecognized cube list field: %s" % key)
    listfile.close()

    #Perform quick validation of input, but only warn for issues
    input_isdir = os.path.isdir(clist["INPUT_DIRECTORY"])
    if not input_isdir:
        warnings.warn("%s is not a directory." % clist["INPUT_DIRECTORY"])

    output_isdir = os.path.isdir(clist["OUTPUT_DIRECTORY"])
    if not output_isdir:
        warnings.warn("%s is not a directory." % clist["OUTPUT_DIRECTORY"])

    try:
        clist["SEARCH_DEPTH"] = int(clist["SEARCH_DEPTH"])
    except:
        raise ValuError("Could not parse SEARCH_DEPTH to int (%s)" % clist["SEARCH_DEPTH"])
    #Return the dictionary
    return clist