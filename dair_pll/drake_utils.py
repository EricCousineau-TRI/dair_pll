"""Drake simulation setup for multibody systems.

This file implements :py:class:`MultibodyPlantDiagram`, which instantiates
Drake simulation and visualization system for a given group of URDF files.

Visualization is done via Drake's VideoWriter. Details on using the VideoWriter
are available in the documentation for :py:mod:`dair_pll.vis_utils`.

In order to make the Drake states compatible with available
:py:class:`~dair_pll.state_space.StateSpace` inheriting classes,
users must define the drake system by a collection of URDF files, each of
which contains a model for exactly one floating- or fixed-base rigid
multibody chain. This allows for the system to be modeled as having a
:py:class:`~dair_pll.state_space.ProductSpace` state space, where each
factor space is a
:py:class:`~dair_pll.state_space.FloatingBaseSpace`
or :py:class:`~dair_pll.state_space.FixedBaseSpace`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple, Dict, List, Optional, Mapping, cast, Union, Type

import numpy as np
import os
import os.path as op

from pydrake.autodiffutils import AutoDiffXd  # type: ignore
from pydrake.geometry import HalfSpace, SceneGraph  # type: ignore
from pydrake.geometry import SceneGraphInspector_, GeometryId  # type: ignore
from pydrake.math import RigidTransform, RollPitchYaw  # type: ignore
from pydrake.multibody.parsing import Parser  # type: ignore
from pydrake.multibody.plant import AddMultibodyPlantSceneGraph  # type: ignore
from pydrake.multibody.plant import CoulombFriction  # type: ignore
from pydrake.multibody.plant import MultibodyPlant  # type: ignore
from pydrake.multibody.plant import MultibodyPlant_  # type: ignore
from pydrake.multibody.tree import ModelInstanceIndex  # type: ignore
from pydrake.multibody.tree import SpatialInertia_  # type: ignore
from pydrake.multibody.tree import world_model_instance, Body_  # type: ignore
from pydrake.symbolic import Expression  # type: ignore
from pydrake.systems.analysis import Simulator  # type: ignore
from pydrake.systems.framework import DiagramBuilder  # type: ignore
from pydrake.visualization import VideoWriter  # type: ignore

from dair_pll import state_space
from dair_pll import file_utils

WORLD_GROUND_PLANE_NAME = "world_ground_plane"
DRAKE_MATERIAL_GROUP = 'material'
DRAKE_FRICTION_PROPERTY = 'coulomb_friction'
N_DRAKE_FLOATING_BODY_VELOCITIES = 6
DEFAULT_DT = 1e-3

GROUND_COLOR = np.array([0.5, 0.5, 0.5, 0.1])

CAM_FOV = np.pi/5
VIDEO_PIXELS = [480, 640]
FPS = 30

# TODO currently hard-coded camera pose could eventually be dynamically chosen
# to fit the actual trajectory.
SENSOR_POSE = RigidTransform(RollPitchYaw([-np.pi/2, 0, np.pi/2]), [2, 0, 0.2])

DrakeTemplateType = Mapping[Type, Type]
MultibodyPlant_ = cast(DrakeTemplateType, MultibodyPlant_)
Body_ = cast(DrakeTemplateType, Body_)
SceneGraphInspector_ = cast(DrakeTemplateType, SceneGraphInspector_)
SpatialInertia_ = cast(DrakeTemplateType, SpatialInertia_)

#:
DrakeMultibodyPlant = Union[MultibodyPlant_[float], MultibodyPlant_[AutoDiffXd],
                            MultibodyPlant_[Expression]]
#:
DrakeBody = Union[Body_[float], Body_[AutoDiffXd], Body_[Expression]]

#:
DrakeSceneGraphInspector = Union[SceneGraphInspector_[float],
                                 SceneGraphInspector_[AutoDiffXd]]
#:
DrakeSpatialInertia = Union[SpatialInertia_[float], SpatialInertia_[AutoDiffXd],
                            SpatialInertia_[Expression]]
#:
UniqueBodyIdentifier = str


def get_bodies_in_model_instance(
        plant: DrakeMultibodyPlant,
        model_instance_index: ModelInstanceIndex) -> List[DrakeBody]:
    """Get list of body names associated with model instance.

    Args:
        plant:
        model_instance_index:
    """
    body_indices = plant.GetBodyIndices(model_instance_index)
    return [plant.get_body(body_index) for body_index in body_indices]


def get_body_names_in_model_instance(
        plant: DrakeMultibodyPlant,
        model_instance_index: ModelInstanceIndex) -> List[str]:
    """Get list of body names associated with model instance."""
    bodies = get_bodies_in_model_instance(plant, model_instance_index)
    return [body.name() for body in bodies]


def unique_body_identifier(plant: DrakeMultibodyPlant,
                           body: DrakeBody) -> UniqueBodyIdentifier:
    """Unique string identifier for given ``Body_``."""
    return f'{plant.GetModelInstanceName(body.model_instance())}_{body.name()}'


def get_all_bodies(
    plant: DrakeMultibodyPlant, model_instance_indices: List[ModelInstanceIndex]
) -> Tuple[List[Body_], List[UniqueBodyIdentifier]]:
    """Get all bodies in plant's models."""
    bodies = []
    for model_instance_index in model_instance_indices:
        bodies.extend(get_bodies_in_model_instance(plant, model_instance_index))
    return bodies, [unique_body_identifier(plant, body) for body in bodies]


def get_all_inertial_bodies(
    plant: DrakeMultibodyPlant, model_instance_indices: List[ModelInstanceIndex]
) -> Tuple[List[DrakeBody], List[UniqueBodyIdentifier]]:
    """Get all bodies that should have inertial parameters in plant."""
    return get_all_bodies(plant, [
        model_index for model_index in model_instance_indices
        if model_index != world_model_instance()
    ])


@dataclass
class CollisionGeometrySet:
    r""":py:func:`dataclasses.dataclass` for tracking object collisions."""
    ids: List[GeometryId] = field(default_factory=list)
    r"""List of geometries that may collide."""
    frictions: List[CoulombFriction] = field(
        default_factory=dict)  # type: ignore
    r"""List of coulomb friction coefficients for the geometries."""
    collision_candidates: List[Tuple[int, int]] = field(
        default_factory=dict)  # type: ignore
    r"""Pairs of geometries that may collide."""


def get_collision_geometry_set(
        inspector: DrakeSceneGraphInspector) -> CollisionGeometrySet:
    """Get colliding geometries, frictional properties, and corresponding
    collision pairs in a scene.

    Args:
        inspector: Inspector of scene graph.

    Returns:
        List of geometries that are candidates for at least one collision.
        Pairs of indices in geometry list that potentially collide.
    """
    geometry_ids: List[GeometryId] = []
    geometry_pairs: List[Tuple[int, int]] = []
    coulomb_frictions: List[CoulombFriction] = []

    for geometry_id_a, geometry_id_b in inspector.GetCollisionCandidates():
        for geometry_id in [geometry_id_a, geometry_id_b]:
            if geometry_id not in geometry_ids:
                geometry_ids.append(geometry_id)
        geometry_index_a = geometry_ids.index(geometry_id_a)
        geometry_index_b = geometry_ids.index(geometry_id_b)
        geometry_pairs.append((geometry_index_a, geometry_index_b))

    for geometry_id in geometry_ids:
        proximity_properties = inspector.GetProximityProperties(geometry_id)
        coulomb_frictions.append(
            proximity_properties.GetProperty(DRAKE_MATERIAL_GROUP,
                                             DRAKE_FRICTION_PROPERTY))

    return CollisionGeometrySet(ids=geometry_ids,
                                frictions=coulomb_frictions,
                                collision_candidates=geometry_pairs)


def add_plant_from_urdfs(
        builder: DiagramBuilder, urdfs: Dict[str, str], dt: float
) -> Tuple[List[ModelInstanceIndex], MultibodyPlant, SceneGraph]:
    """Add plant to builder with prescribed URDF models.

    Generates a world containing each given URDF as a model instance.

    Args:
        builder: Diagram builder to add plant to
        urdfs: Names and corresponding URDFs to add as models to plant.
        dt: Time step of plant in seconds.

    Returns:
        Named dictionary of model instances returned by
        ``AddModelFromFile``.
        New plant, which has been added to builder.
        Scene graph associated with new plant.
    """
    plant, scene_graph = AddMultibodyPlantSceneGraph(builder, dt)
    parser = Parser(plant)

    # Build [model instance index] list, starting with world model, which is
    # always added by default.
    model_ids = [world_model_instance()]
    model_ids.extend(
        [parser.AddModelFromFile(urdf, name) for name, urdf in urdfs.items()])

    return model_ids, plant, scene_graph


class MultibodyPlantDiagram:
    """Constructs and manages a diagram, simulator, and optionally a visualizer
    for a multibody system described in a list of URDF's.

    This minimal diagram consists of a ``MultibodyPlant``, ``SceneGraph``, and
    optionally a ``VideoWriter`` hooked up in the typical fashion.

    From the ``MultibodyPlant``, ``MultibodyPlantDiagram`` can infer the
    corresponding ``StateSpace`` from the dimension of the associated
    velocity vectors in the plant's context, via the one-chain-per-file
    assumption.
    """
    # pylint: disable=too-few-public-methods
    sim: Simulator
    plant: MultibodyPlant
    scene_graph: SceneGraph
    visualizer: Optional[VideoWriter]
    model_ids: List[ModelInstanceIndex]
    collision_geometry_set: CollisionGeometrySet
    space: state_space.ProductSpace

    def __init__(self,
                 urdfs: Dict[str, str],
                 dt: float = DEFAULT_DT,
                 enable_visualizer: bool = False) -> None:
        r"""Initialization generates a world containing each given URDF as a
        model instance, and a corresponding Drake ``Simulator`` set up to
        trigger a state update every ``dt``.

        By default, a ground plane is added at world height ``z = 0``.

        Args:
            urdfs: Names and corresponding URDFs to add as models to plant.
            dt: Time step of plant in seconds.
            enable_visualizer: Whether to add visualization system to diagram.
        """
        builder = DiagramBuilder()
        model_ids, plant, scene_graph = add_plant_from_urdfs(builder, urdfs, dt)

        # Add visualizer to diagram if enabled. Sets ``delete_prefix_on_load``
        # to False, in the hopes of saving computation time; may cause
        # re-initialization to produce erroneous visualizations.
        visualizer = None
        if enable_visualizer:
            visualizer = VideoWriter.AddToBuilder(
                filename=file_utils.get_experiment_video_filename(),
                builder=builder, sensor_pose=SENSOR_POSE, fps=FPS,
                width=VIDEO_PIXELS[1], height=VIDEO_PIXELS[0], fov_y=CAM_FOV)

        # Adds ground plane at ``z = 0``
        halfspace_transform = RigidTransform()
        friction = CoulombFriction(1.0, 1.0)
        plant.RegisterCollisionGeometry(plant.world_body(), halfspace_transform,
                                        HalfSpace(), WORLD_GROUND_PLANE_NAME,
                                        friction)
        plant.RegisterVisualGeometry(plant.world_body(), halfspace_transform,
                                     HalfSpace(), WORLD_GROUND_PLANE_NAME,
                                     GROUND_COLOR)

        # get collision candidates before default context filters for proximity.
        self.collision_geometry_set = get_collision_geometry_set(
            scene_graph.model_inspector())

        # Builds and initialize simulator from diagram
        plant.Finalize()
        diagram = builder.Build()
        diagram.CreateDefaultContext()
        sim = Simulator(diagram)
        sim.Initialize()
        sim.set_publish_every_time_step(False)

        self.sim = sim
        self.plant = plant
        self.scene_graph = scene_graph
        self.visualizer = visualizer
        self.model_ids = model_ids
        self.space = self.generate_state_space()

    def generate_state_space(self) -> state_space.ProductSpace:
        """Generate ``StateSpace`` object for plant.

        Under the one-chain-per-model assumption, iteratively constructs a
        ``ProductSpace`` representation for the state of the ``MultibodyPlant``.

        Returns:
            State space of the diagram's underlying multibody system.
        """
        plant = self.plant

        spaces = []  # type: List[state_space.StateSpace]
        for model_id in self.model_ids:
            if plant.HasUniqueFreeBaseBody(model_id):
                # Ensures quaternion is used to model rotation, instead of
                # XYZMobilizer, for instance.
                free_body = plant.GetUniqueFreeBaseBodyOrThrow(model_id)
                assert free_body.has_quaternion_dofs()

                n_joints = plant.num_velocities(
                    model_id) - N_DRAKE_FLOATING_BODY_VELOCITIES
                spaces.append(state_space.FloatingBaseSpace(n_joints))
            else:
                n_joints = plant.num_velocities(model_id)
                spaces.append(state_space.FixedBaseSpace(n_joints))

        return state_space.ProductSpace(spaces)
