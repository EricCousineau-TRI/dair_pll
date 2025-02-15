r"""Classes for generating and managing datasets for experiments.

Centers around the :class:`SystemDataManager` type, which transforms a set of
trajectories saved to disc for various tasks encountered during an
experiment. This module also contains utilities for generating simulation data
from a :class:`~dair_pll.system.System`\ ."""
import time
from dataclasses import dataclass
from typing import List, Tuple, Union, Type, Optional

import torch
from torch import Tensor
from torch.utils.data import Dataset

from dair_pll import file_utils
from dair_pll.state_space import CenteredSampler, \
    GaussianWhiteNoiser, UniformSampler, UniformWhiteNoiser
from dair_pll.system import System


@dataclass
class DataGenerationConfig:
    """:func:`~dataclasses.dataclass` for configuring generation of a
    trajectory dataset."""

    # pylint: disable=too-many-instance-attributes
    n_pop: float = 16384
    r"""Total number of trajectories to select from, ``>= 0``\ ."""
    traj_len: int = 80
    r"""Trajectory length, ``>= 1``\ ."""
    x_0: Tensor = Tensor()
    """Nominal initial states."""
    sampler_type: Type[CenteredSampler] = UniformSampler
    r"""Distribution for sampling around :attr:`x_0`\ ."""
    sampler_ranges: Tensor = Tensor()
    r"""``(2 * n_v)`` size of perturbations sampled around :attr:`x_0`\ ."""
    noiser_type: Union[Type[GaussianWhiteNoiser], Type[UniformWhiteNoiser]] = \
        GaussianWhiteNoiser
    """Type of noise to add to data."""
    static_noise: Tensor = Tensor()
    """``(2 * n_v)`` sampler ranges for constant-in-time trajectory noise."""
    dynamic_noise: Tensor = Tensor()
    """``(2 * n_v)`` sampler ranges for i.i.d.-in-time trajectory noise."""


@dataclass
class DataConfig:
    """:func:`~dataclasses.dataclass` for configuring a trajectory dataset."""

    # pylint: disable=too-many-instance-attributes
    storage: str = './'
    """Folder to store results in and load data from."""
    dt: float = 1e-3
    r"""Time step, ``> 0``\ ."""
    train_fraction: float = 0.5
    r"""Fraction of training trajectories to select, ``<= 1, >= 0``\ ."""
    valid_fraction: float = 0.25
    r"""Fraction of validation trajectories to select, ``<= 1, >= 0``\ ."""
    test_fraction: float = 0.25
    r"""Fraction of testing trajectories to select, ``<= 1, >= 0``\ ."""
    t_skip: int = 0
    """Index of first time to predict from ``>=`` :attr:`t_history` ``- 1``."""
    t_history: int = 1
    r"""Number of steps in initial condition for prediction, ``>= 1``\ ."""
    t_prediction: int = 1
    r"""Number of future steps to use during training/evaluation, ``>= 1``\ ."""
    generation_config: Optional[DataGenerationConfig] = None
    """Optionally, signals generation of data from given system."""
    import_directory: Optional[str] = None
    """Alternatively, signals data import from separate directory."""
    dynamic_updates_from: Optional[int] = None
    """Alternatively, loads dynamically, but blocks on initial size set."""


class TrajectorySliceDataset(Dataset):
    """Dataset of trajectory slices for training.

    Given a list of trajectories

    """
    in_slices: List[Tensor]
    out_slices: List[Tensor]

    def __init__(self, trajectories: List[Tensor], t_skip: int = 0,
                 t_history: int = 1, t_prediction: int = 1):
        """Initialization:

        Args:
            trajectories: test
            t_skip:
            t_history:
            t_prediction:
        """
        assert t_skip + 1 >= t_history
        self.t_skip = t_skip
        self.t_history = t_history
        self.t_prediction = t_prediction
        self.in_slices  = [] # type: List[Tensor]
        self.out_slices = []  # type: List[Tensor]
        for trajectory in trajectories:
            self.add_sliced_trajectory(trajectory)

    def add_sliced_trajectory(self, traj: Tensor) -> Tuple[
        List[Tensor], List[Tensor]]:
        traj_len = traj.shape[0]
        first = self.t_skip
        last = traj_len - self.t_prediction
        in_window = self.t_history
        out_window = self.t_prediction
        assert first <= last
        for i in range(first, last):
            self.in_slices.append(traj[(i + 1 - in_window):(i + 1), :])
            self.out_slices.append(traj[(i + 1):(i + 1 + out_window), :])

    def __len__(self) -> int:
        return len(self.in_slices)

    def __getitem__(self, idx) -> Tuple[Tensor, Tensor]:
        return self.in_slices[idx], self.out_slices[idx]


@dataclass
class TrajectorySet:
    slices: TrajectorySliceDataset
    trajectories: List[Tensor]


class SystemDataManager:
    system: System
    config: DataConfig
    train_set: TrajectorySet
    valid_set: TrajectorySet
    test_set: TrajectorySet
    n_on_disk: int

    def __init__(self, system: System, config: DataConfig) -> None:
        self.system = system
        self.config = config

        # ensure only one data source
        do_generate = config.generation_config is not None
        do_import = config.import_directory is not None
        do_dynamics = config.dynamic_updates_from is not None
        assert (int(do_generate) + int(do_import) + int(do_dynamics)) == 1
        if do_generate:
            self.generate()

        elif do_import:
            file_utils.import_data_to_storage(config.storage,
                                              config.import_directory)

        else:
            print("Waiting for minimum trajectory count...")
            n_on_disk = file_utils.get_trajectory_count(self.config.storage)
            while n_on_disk < config.dynamic_updates_from:
                n_on_disk = file_utils.get_trajectory_count(self.config.storage)
                time.sleep(1)
            print("Minimum trajectory count reached!")

        self.n_on_disk = file_utils.get_trajectory_count(self.config.storage)

        self.get_trajectory_split()

    def get_tensorboard_folder(self) -> str:
        return file_utils.tensorboard_dir(self.config.storage)

    def generate_trajectory_set(self, N: int, T: int) -> List[
        Tensor]:
        assert N >= 0
        assert T >= 1
        config = self.config
        assert config.generation_config is not None
        generation_config = config.generation_config
        system = self.system
        starting_state = generation_config.x_0
        system.set_state_sampler(
            generation_config.sampler_type(system.space,
                                           generation_config.sampler_ranges,
                                           x_0=starting_state))

        trajectories = []
        for i in range(N):
            xtraj, carry = system.sample_trajectory(T)
            trajectories.append(xtraj)
        return self.noised_trajectories(trajectories)

    def make_trajectory_set(self, trajectories: List[Tensor]) -> TrajectorySet:
        config = self.config
        slice_dataset = TrajectorySliceDataset(
            trajectories,
            config.t_skip,
            config.t_history,
            config.t_prediction
        )
        return TrajectorySet(slices=slice_dataset, trajectories=trajectories)

    def generate(self) -> None:
        config = self.config
        assert config.generation_config is not None
        traj_len = config.generation_config.traj_len
        n_pop = config.generation_config.n_pop
        n_generated = file_utils.get_trajectory_count(config.storage)
        n_set = 30
        while n_generated < n_pop:
            traj_i = self.generate_trajectory_set(n_set, traj_len)
            n_generated = file_utils.get_trajectory_count(config.storage)
            if n_generated == n_pop:
                break
            n_set = min(n_set, n_pop - n_generated)
            for i in range(n_set):
                torch.save(
                    traj_i[i],
                    file_utils.trajectory_file(config.storage,
                                               n_generated + i)
                )

    def get_trajectories(self, N_begin: int, N_end: int, N_requested: int) -> List[
        Tensor]:
        n_on_disk = self.n_on_disk
        assert n_on_disk >= N_end
        trajectory_order = torch.randperm(N_end - N_begin) + N_begin
        selection = trajectory_order[:N_requested]

        data = [torch.load(file_utils.trajectory_file(self.config.storage, i))
                for i in selection]
        return data

    def noised_trajectories(self, traj_set: List[Tensor]):
        config = self.config
        assert config.generation_config is not None
        generation_config = config.generation_config
        noiser = generation_config.noiser_type(self.system.space)
        noised_trajectories = []
        for traj in traj_set:
            static_disturbed = noiser.noise(traj,
                                            generation_config.static_noise,
                                            independent=False)
            dynamic_disturbed = noiser.noise(static_disturbed,
                                             generation_config.dynamic_noise)
            dynamic_disturbed = self.system.space.project_derivative(
                dynamic_disturbed, config.dt)
            noised_trajectories.append(dynamic_disturbed)
        return noised_trajectories

    def get_trajectory_split(self) -> Tuple[TrajectorySet, TrajectorySet,
                                            TrajectorySet]:

        config = self.config
        n_on_disk = self.n_on_disk
        if not hasattr(self, 'train_set'):
            N_train = round(n_on_disk * config.train_fraction)
            N_valid = round(n_on_disk * config.valid_fraction)
            N_test = round(n_on_disk * config.test_fraction)
            N_test = min(N_test, n_on_disk - N_train - N_valid)
            N_tot = sum([N_train, N_test, N_valid])

            traj_set = self.get_trajectories(0, n_on_disk, N_tot)

            train_traj = traj_set[:N_train]
            traj_set = traj_set[N_train:]

            valid_traj = traj_set[:N_valid]
            test_traj = traj_set[N_valid:]

            self.train_set = self.make_trajectory_set(train_traj)
            self.valid_set = self.make_trajectory_set(valid_traj)
            self.test_set = self.make_trajectory_set(test_traj)
        elif config.dynamic_updates_from:
            # update trajectory sets
            n_on_disk_new = file_utils.get_trajectory_count(config.storage)
            if n_on_disk_new != n_on_disk:
                new_count = n_on_disk_new - n_on_disk
                self.n_on_disk = n_on_disk_new
                N_train = round(new_count * config.train_fraction)
                N_valid = round(new_count * config.valid_fraction)
                N_test = round(new_count * config.test_fraction)
                N_tot = sum([N_train, N_test, N_valid])
                new_traj_set = self.get_trajectories(n_on_disk, n_on_disk_new,
                                                     N_tot)
                train_traj = new_traj_set[:N_train]
                new_traj_set = new_traj_set[N_train:]

                valid_traj = new_traj_set[:N_valid]
                test_traj = new_traj_set[N_valid:]


                sets = (self.train_set, self.valid_set, self.test_set)
                trajectory_lists = (train_traj, valid_traj, test_traj)
                for set, trajectory_list in zip(sets, trajectory_lists):
                    set.trajectories.extend(trajectory_list)
                    for trajectory in trajectory_list:
                        set.slices.add_sliced_trajectory(trajectory)
        return self.train_set, self.valid_set, self.test_set
