"""Utility functions for managing saved files for training models.

File system is organized around a "storage" directory associated with data and
training runs. The functions herein can be used to return absolute paths of and
summary information about the content of this directory.
"""
import glob
import os
import time
from os import path
from typing import List, Optional

TRAJ_EXTENSION = '.pt'  # trajectory file
HYPERPARAMETERS_EXTENSION = '.json'  # hyperparameter set file
STATS_EXTENSION = '.pkl'  # experiment statistics
"""str: extensions for saved files"""


def assure_created(directory: str) -> str:
    """Wrapper to put around directory paths which ensure their existence.

    Args:
        directory: Path of directory that may not exist.

    Returns:
        ``directory``, Which is ensured to exist by recursive mkdir.
    """
    directory = path.abspath(directory)
    if not path.exists(directory):
        assure_created(path.dirname(directory))
        os.mkdir(directory)
    return directory


MAIN_DIR = path.dirname(path.dirname(__file__))
RESULTS_DIR = assure_created(os.path.join(MAIN_DIR, 'results'))
ASSETS_DIR = assure_created(os.path.join(MAIN_DIR, 'assets'))
PLOTS_DIR = assure_created(os.path.join(MAIN_DIR, 'plots'))
# str: locations of key static directories


def get_asset(asset_file_basename: str) -> str:
    """Gets

    Args:
        asset_file_basename: Basename of asset file located in ``ASSET_DIR``

    Returns:
        Asset's absolute path.
    """
    return os.path.join(ASSETS_DIR, asset_file_basename)


# deprecated
def study_dir(study_name: str) -> str:
    """(Deprecated) old name for storage directory

    Args:
        study_name: storage name

    Returns:
        Absolute path of storage directory
    Warnings:
        Deprecated in favor of :func:`storage_dir`
    """
    return storage_dir(study_name)


def assure_storage_tree_created(storage_name: str) -> None:
    """Assure that all subdirectories of specified storage are created.

    Args:
        storage_name: name of storage directory.
    """
    storage_directories = [
        urdf_dir,
        data_dir,
        tensorboard_dir,
        temp_dir
    ]  # type: List[Callable[[str],str]]

    for directory in storage_directories:
        assure_created(directory(storage_name))


def import_data_to_storage(storage_name: str, import_data_dir: str) -> None:
    """Import data in external folder into data directory.

    Args:
        storage_name: Name of storage for data import.
        import_data_dir: Directory to import data from.
    """
    # check if data is synchronized already
    storage_data_dir = data_dir(storage_name)
    storage_traj_count = get_numeric_file_count(storage_data_dir,
                                                TRAJ_EXTENSION)
    data_traj_count = get_numeric_file_count(import_data_dir, TRAJ_EXTENSION)

    # overwrite in case of any discrepeancies
    if storage_traj_count != data_traj_count:
        os.system(f'rm -r {storage_data_dir}')
        os.system(f'cp -r {import_data_dir} {storage_data_dir}')


def storage_dir(storage_name: str) -> str:
    """Absolute path of storage directory"""
    # return assure_created(os.path.join(RESULTS_DIR, storage_name))
    return assure_created(storage_name)


def urdf_dir(storage_name: str) -> str:
    """Absolute path of learned model URDF storage directory"""
    return assure_created(path.join(storage_dir(storage_name), 'urdf'))


def data_dir(storage_name: str) -> str:
    """Absolute path of training/validation/test trajectories directory"""
    return assure_created(path.join(storage_dir(storage_name), 'data'))


def tensorboard_dir(storage_name: str) -> str:
    """Absolute path of tensorboard storage folder"""
    return assure_created(path.join(storage_dir(storage_name), 'tensorboard'))


def temp_dir(storage_name: str) -> str:
    """Absolute path of temp storage folder"""
    return assure_created(path.join(storage_dir(storage_name), 'temp'))


def wait_for_temp(storage_name: str,
                  basename_regex: str = "*",
                  duration: float = 20) -> Optional[str]:
    """Waits for unique temporary file matching regular expression to appear.

    Args:
        storage_name: Name of storage folder
        basename_regex: Regular expression to isolate basename
        duration: Wait duration in seconds

    Returns:
        file basename if it exists and is unique at any time before duration
        runs out.
    """
    start = time.time()
    search_dir = temp_dir(storage_name)
    while time.time() - start < duration:
        files = glob.glob1(search_dir, basename_regex)
        if len(files) == 1:
            return files[0]
    return None


def delete(file_name: str) -> None:
    """Removes file at path specified by ``file_name``"""
    if path.exists(file_name):
        os.remove(file_name)


def hyperparameter_file(storage_name: str) -> str:
    """Absolute path of optimized hyperparameters for a study"""
    return path.join(storage_dir(storage_name),
                     f'optimized_hyperparameters{HYPERPARAMETERS_EXTENSION}')


def get_numeric_file_count(directory: str,
                           extension: str = TRAJ_EXTENSION) -> int:
    """Count number of whole-number-named files.

    If folder ``/fldr`` has contents (7.pt, 11.pt, 4.pt), then::

        get_numeric_file_count("/fldr", ".pt") == 3

    Args:
        directory: Directory to tally file count in
        extension: Extension of files to be counted

    Returns:
        Number of files in specified ``directory`` with specified
        ``extension`` that have an integer basename.
    """
    return len(glob.glob(path.join(directory, './[0-9]*' + extension)))


def get_trajectory_count(storage_name: str):
    """Count number of trajectories on disc in storage directory"""
    return get_numeric_file_count(data_dir(storage_name), TRAJ_EXTENSION)


def get_sweep_summary_count(storage_name: str, n_train: int):
    """Count number of completed samples of dataset size in sweep"""
    return get_numeric_file_count(sweep_dir(storage_name, n_train),
                                  STATS_EXTENSION)


def trajectory_file(storage_name: str, num_trajectory: int) -> str:
    """Absolute path of specific trajectory in storage"""
    return path.join(data_dir(storage_name),
                     f'{num_trajectory}{TRAJ_EXTENSION}')


def append_by_extension(directory: str, extension: str = TRAJ_EXTENSION) -> str:
    """Get absolute path of a new numeric file name for saving.

    At various times, we generate a sequence of N files. This method is a
    helper function that allows a worker to pick a file name to append to the
    previously generated files.

    Example::

        # /fldr has contents (0.pt, 1.pt, 2.pt)
        append_by_extension("/fldr", ".pt") == '/fldr/3.pt'

    Args:
        directory: Directory in which file will be saved
        extension: Extension of desired file name

    Returns:
        Absolute path of new file name

    Warnings:
        Doesn't actually check that previous files are cleanly ordered from 0
        upwards.
        Isn't thread safe by design; race condition is possible where
        multiple workers save to the same file name as a result.

    Todo:
        Make thread-safe version
    """
    num_files = get_numeric_file_count(assure_created(directory), extension)
    return path.join(directory, f'{num_files}{extension}')


def sweep_dir(storage_name: str, n_train: int) -> str:
    """Returns directory of summary statistics for samples of dataset size sweep

    Args:
        storage_name: Name of storage folder
        n_train: Training set size of requested samples

    Returns:
        Absolute path where summary statistics are stored.
    """
    return assure_created(path.join(storage_dir(storage_name), str(n_train)))


def sweep_data_sizes(storage_name) -> List[int]:
    """Lists dataset sizes with completed summaries associated with a sweep"""
    folders = glob.glob(path.join(storage_dir(storage_name), './[0-9]*'))
    ints = [int(path.basename(f)) for f in folders]
    ints.sort()
    return ints


def sweep_summary_file(storage_name: str,
                       n_train: int,
                       n_run: Optional[int] = None) -> str:
    """File name for specific sample of dataset size sweep

    Args:
        storage_name: Name of storage folder
        n_train: Training set size of requested sample
        n_run: Specific sample number

    Returns:
        Absolute path of summary statistics file for specified sample.
    """
    directory = assure_created(sweep_dir(storage_name, n_train))
    if n_run is None:
        return append_by_extension(directory, STATS_EXTENSION)
    return path.join(directory, str(n_run) + STATS_EXTENSION)


def experiment_storage_dir() -> str:
    """Based on the value of the environment variable ``PLL_EXPERIMENT``, return
    the default storage directory.

    Notes:
        This is a bit of a hack.  TODO can build in a more elegant solution in
        the future.
    """
    exp_name = os.environ['PLL_EXPERIMENT'] if 'PLL_EXPERIMENT' in os.environ \
               else ''
    return assure_created(path.join(RESULTS_DIR, exp_name))


def get_experiment_video_filename() -> str:
    """Return the filepath of the temporary rollout video gif.  This calls
    :py:func:`experiment_storage_dir`, which relies on the environment variable
    ``PLL_EXPERIMENT`` to be set."""
    return path.join(temp_dir(experiment_storage_dir()), 'output.gif')


def get_experiment_urdf_dir() -> str:
    """Return the filepath of the urdf directory for the current experiment.
    This calls :py:func:`experiment_storage_dir`, which relies on the
    environment variable ``PLL_EXPERIMENT`` to be set."""
    return urdf_dir(experiment_storage_dir())

