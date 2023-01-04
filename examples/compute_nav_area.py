#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
Manually control the robot to interact with the environment. Run as
```
python examples/interative_play.py
```

To Run you need PyGame installed (to install run `pip install pygame==2.0.1`).

By default this controls with velocity control (which makes controlling the
robot hard). To use IK control instead add the `--add-ik` command line argument.

Controls:
- For velocity control
    - 1-7 to increase the motor target for the robot arm joints
    - Q-U to decrease the motor target for the robot arm joints
- For IK control
    - W,S,A,D to move side to side
    - E,Q to move up and down
- I,J,K,L to move the robot base around
- PERIOD to print the current world coordinates of the robot base.
- Z to toggle the camera to free movement mode. When in free camera mode:
    - W,S,A,D,Q,E to translate the camera
    - I,J,K,L,U,O to rotate the camera
    - B to reset the camera position
- X to change the robot that is being controlled (if there are multiple robots).

Change the task with `--cfg configs/tasks/rearrange/close_cab.yaml` (choose any task under the `configs/tasks/rearrange/` folder).

Change the grip type:
- Suction gripper `TASK.ACTIONS.ARM_ACTION.GRIP_CONTROLLER "SuctionGraspAction"`

To record a video: `--save-obs` This will save the video to file under `data/vids/` specified by `--save-obs-fname` (by default `vid.mp4`).

Record and play back trajectories:
- To record a trajectory add `--save-actions --save-actions-count 200` to
  record a truncated episode length of 200.
- By default the trajectories are saved to data/interactive_play_replays/play_actions.txt
- Play the trajectories back with `--load-actions data/interactive_play_replays/play_actions.txt`
"""

import argparse
import copy
import os
import pickle
import sys
import time
from collections import defaultdict

import magnum as mn
import matplotlib.pyplot as plt
import numpy as np

import habitat
import habitat.tasks.rearrange.rearrange_task
import habitat_sim
from habitat.core.logging import logger
from habitat.tasks.rearrange.actions.actions import ArmEEAction
from habitat.tasks.rearrange.rearrange_sensors import GfxReplayMeasure
from habitat.tasks.rearrange.utils import euler_to_quat, write_gfx_replay
from habitat.utils.render_wrapper import overlay_frame
from habitat.utils.visualizations.utils import observations_to_image
from habitat_sim.utils import viz_utils as vut

sys.setrecursionlimit(10000000)


try:
    import pygame
except ImportError:
    pygame = None

SAVE_NAME = "spot_final"
DEFAULT_CFG = "configs/tasks/rearrange/check_nav_spot.yaml"
# DEFAULT_CFG = "configs/tasks/rearrange/check_nav_stretch.yaml"
# DEFAULT_CFG = "configs/tasks/rearrange/check_nav_fetch.yaml"
DEFAULT_RENDER_STEPS_LIMIT = 60
SAVE_VIDEO_DIR = "./data/vids"
SAVE_ACTIONS_DIR = "./data/interactive_play_replays"

import os

# Quiet the Habitat simulator logging
os.environ["MAGNUM_LOG"] = "quiet"
os.environ["HABITAT_SIM_LOG"] = "quiet"


def object_copy(instance, init_args=None):
    if init_args:
        new_obj = instance.__class__(**init_args)
    else:
        new_obj = habitat.Env(config=init_args)
    if hasattr(instance, "__dict__"):
        for k in instance.__dict__:
            try:
                attr_copy = copy.deepcopy(getattr(instance, k))
            except Exception as e:
                attr_copy = object_copy(getattr(instance, k))
            setattr(new_obj, k, attr_copy)

        new_attrs = list(new_obj.__dict__.keys())
        for k in new_attrs:
            if not hasattr(instance, k):
                delattr(new_obj, k)
        return new_obj
    else:
        return instance


def step_env(env, action_name, action_args):
    return env.step({"action": action_name, "action_args": action_args})


def get_input_vel_ctlr(
    skip_pygame,
    arm_action,
    env,
    not_block_input,
    agent_to_control,
    base_action,
):

    if skip_pygame:
        return step_env(env, "EMPTY", {}), None, False
    multi_agent = len(env._sim.robots_mgr) > 1

    arm_action_name = "ARM_ACTION"
    base_action_name = "BASE_VELOCITY"
    arm_key = "arm_action"
    grip_key = "grip_action"
    base_key = "base_vel"
    if multi_agent:
        agent_k = f"AGENT_{agent_to_control}"
        arm_action_name = f"{agent_k}_{arm_action_name}"
        base_action_name = f"{agent_k}_{base_action_name}"
        arm_key = f"{agent_k}_{arm_key}"
        grip_key = f"{agent_k}_{grip_key}"
        base_key = f"{agent_k}_{base_key}"

    arm_action_space = env.action_space.spaces[arm_action_name].spaces[arm_key]
    arm_ctrlr = env.task.actions[arm_action_name].arm_ctrlr

    if arm_action is None:
        arm_action = np.zeros(arm_action_space.shape[0])
        given_arm_action = False
    else:
        given_arm_action = True

    end_ep = False
    magic_grasp = None

    args = {}
    if base_action is not None and base_action_name in env.action_space.spaces:
        name = base_action_name
        args = {base_key: base_action}
    else:
        name = arm_action_name
        if given_arm_action:
            # The grip is also contained in the provided action
            args = {
                arm_key: arm_action[:-1],
                grip_key: arm_action[-1],
            }
        else:
            args = {arm_key: arm_action, grip_key: magic_grasp}

    if magic_grasp is None:
        arm_action = [*arm_action, 0.0]
    else:
        arm_action = [*arm_action, magic_grasp]

    return step_env(env, name, args), arm_action, end_ep


def get_wrapped_prop(venv, prop):
    if hasattr(venv, prop):
        return getattr(venv, prop)
    elif hasattr(venv, "venv"):
        return get_wrapped_prop(venv.venv, prop)
    elif hasattr(venv, "env"):
        return get_wrapped_prop(venv.env, prop)

    return None


class FreeCamHelper:
    def __init__(self):
        self._is_free_cam_mode = False
        self._last_pressed = 0
        self._free_rpy = np.zeros(3)
        self._free_xyz = np.zeros(3)

    @property
    def is_free_cam_mode(self):
        return self._is_free_cam_mode

    def update(self, env, step_result, update_idx):
        keys = pygame.key.get_pressed()
        if keys[pygame.K_z] and (update_idx - self._last_pressed) > 60:
            self._is_free_cam_mode = not self._is_free_cam_mode
            logger.info(f"Switching camera mode to {self._is_free_cam_mode}")
            self._last_pressed = update_idx

        if self._is_free_cam_mode:
            offset_rpy = np.zeros(3)
            if keys[pygame.K_u]:
                offset_rpy[1] += 1
            elif keys[pygame.K_o]:
                offset_rpy[1] -= 1
            elif keys[pygame.K_i]:
                offset_rpy[2] += 1
            elif keys[pygame.K_k]:
                offset_rpy[2] -= 1
            elif keys[pygame.K_j]:
                offset_rpy[0] += 1
            elif keys[pygame.K_l]:
                offset_rpy[0] -= 1

            offset_xyz = np.zeros(3)
            if keys[pygame.K_q]:
                offset_xyz[1] += 1
            elif keys[pygame.K_e]:
                offset_xyz[1] -= 1
            elif keys[pygame.K_w]:
                offset_xyz[2] += 1
            elif keys[pygame.K_s]:
                offset_xyz[2] -= 1
            elif keys[pygame.K_a]:
                offset_xyz[0] += 1
            elif keys[pygame.K_d]:
                offset_xyz[0] -= 1
            offset_rpy *= 0.1
            offset_xyz *= 0.1
            self._free_rpy += offset_rpy
            self._free_xyz += offset_xyz
            if keys[pygame.K_b]:
                self._free_rpy = np.zeros(3)
                self._free_xyz = np.zeros(3)

            quat = euler_to_quat(self._free_rpy)
            trans = mn.Matrix4.from_(
                quat.to_matrix(), mn.Vector3(*self._free_xyz)
            )
            env._sim._sensors[
                "robot_third_rgb"
            ]._sensor_object.node.transformation = trans
            step_result = env._sim.get_sensor_observations()
            return step_result
        return step_result


def distance_angle(alpha, beta):
    alpha = float(alpha)
    beta = float(beta)
    phi = abs(beta - alpha) % (2 * np.pi)
    # This is either the distance or 360 - distance
    if phi > np.pi:
        return 2 * np.pi - phi
    else:
        return phi


def play_env(env, args, config, level, visit_pos_rot, flag_distinct):

    render_steps_limit = None
    if args.no_render:
        render_steps_limit = DEFAULT_RENDER_STEPS_LIMIT

    use_arm_actions = None
    if args.load_actions is not None:
        with open(args.load_actions, "rb") as f:
            use_arm_actions = np.load(f)
            logger.info("Loaded arm actions")

    if not args.no_render:
        draw_obs = observations_to_image(obs, {})
        pygame.init()
        screen = pygame.display.set_mode(
            [draw_obs.shape[1], draw_obs.shape[0]]
        )

    update_idx = 0
    target_fps = 60.0
    prev_time = time.time()
    all_obs = []
    total_reward = 0
    all_arm_actions = []
    agent_to_control = 0

    free_cam = FreeCamHelper()
    gfx_measure = env.task.measurements.measures.get(
        GfxReplayMeasure.cls_uuid, None
    )
    is_multi_agent = len(env._sim.robots_mgr) > 1

    if args.save_actions and len(all_arm_actions) > args.save_actions_count:
        # quit the application when the action recording queue is full
        print("A")
        return
    if render_steps_limit is not None and update_idx > render_steps_limit:
        print("B")
        return

    if args.no_render:
        keys = defaultdict(lambda: False)
    else:
        keys = pygame.key.get_pressed()

    if not args.no_render and is_multi_agent and keys[pygame.K_x]:
        agent_to_control += 1
        agent_to_control = agent_to_control % len(env._sim.robots_mgr)
        logger.info(
            f"Controlled agent changed. Controlling agent {agent_to_control}."
        )

    before_base_pos = env.sim.robot.base_pos
    before_base_rot = env.sim.robot.base_rot

    base_vel_ctrl = habitat_sim.physics.VelocityControl()
    base_vel_ctrl.controlling_lin_vel = True
    base_vel_ctrl.lin_vel_is_local = True
    base_vel_ctrl.controlling_ang_vel = True
    base_vel_ctrl.ang_vel_is_local = True

    is_navigable = env.sim.pathfinder.is_navigable(before_base_pos)
    is_contact = env.sim.contact_test(env.sim.robot.get_robot_sim_id())

    if (not is_navigable) or (is_contact):
        if level % 100 == 0:
            print("collide:", level)
        return 0

    if not flag_distinct:
        if level % 100 == 0:
            print("repeat:", level)
        return 0

    if level % 100 == 0:
        print("preceed:", level)

    ans = 1
    for base_action in [[0, 1], [1, 0]]:

        env.sim.robot.base_pos = before_base_pos
        env.sim.robot.base_rot = before_base_rot

        lin_vel = base_action[0] * 10
        ang_vel = (
            base_action[1] * 50
        )  # This one needs to be big so that the agent can navigate from places
        # work v2: 10 and 100
        base_vel_ctrl.linear_velocity = mn.Vector3(lin_vel, 0, 0)
        base_vel_ctrl.angular_velocity = mn.Vector3(0, ang_vel, 0)

        trans = env.sim.robot.sim_obj.transformation
        rigid_state = habitat_sim.RigidState(
            mn.Quaternion.from_matrix(trans.rotation()), trans.translation
        )
        target_rigid_state = base_vel_ctrl.integrate_transform(
            1 / env.sim.ctrl_freq, rigid_state
        )

        new_pos = target_rigid_state.translation
        new_pos[1] = env.sim.robot.base_pos[1]
        env.sim.robot.base_pos = new_pos
        env.sim.robot.base_rot = target_rigid_state.rotation.angle()

        after_base_pos = env.sim.robot.base_pos
        after_base_rot = env.sim.robot.base_rot

        # diff_pos = before_base_pos - after_base_pos
        # diff_rot = before_base_rot - after_base_rot

        # diff_value_current = 0
        # for i in range(3):
        #     diff_value_current += abs(diff_pos[i])
        # diff_value_current += abs(float(diff_rot))

        # set the flag.
        flag_distinct = True
        has_visited_threshold_pos = 0.01  # 0.08333328578069245
        has_visited_threshold_rot = 0.01  # 0.08333349227905273

        # work v2: 0.05 and 0.05

        for point in visit_pos_rot:
            pos, rot = point
            diff_vec = np.array(pos) - np.array(vec2list(after_base_pos))
            diff_pos = 0
            for i in range(3):
                diff_pos += diff_vec[i] ** 2
            diff_pos = diff_pos**0.5
            diff_rot = distance_angle(rot, after_base_rot)
            if (
                diff_pos <= has_visited_threshold_pos
                and diff_rot <= has_visited_threshold_rot
            ):
                flag_distinct = False
                break

        if flag_distinct:
            visit_pos_rot.append(
                [vec2list(after_base_pos), float(after_base_rot)]
            )
        ans += play_env(
            env, args, config, level + 1, visit_pos_rot, flag_distinct
        )

    return ans
    # if end_ep:
    #     total_reward = 0
    #     # Clear the saved keyframes.
    #     if gfx_measure is not None:
    #         gfx_measure.get_metric(force_get=True)
    #     env.reset()

    if not args.no_render:
        pygame.event.pump()
    if env.episode_over:
        import pdb

        pdb.set_trace()
        total_reward = 0
        env.reset()

    if not args.no_render:
        pygame.quit()


def has_pygame():
    return pygame is not None


def vec2list(v):
    return [v[0], v[1], v[2]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-render", action="store_true", default=False)
    parser.add_argument("--save-obs", action="store_true", default=False)
    parser.add_argument("--save-obs-fname", type=str, default="play.mp4")
    parser.add_argument("--save-actions", action="store_true", default=False)
    parser.add_argument(
        "--save-actions-fname", type=str, default="play_actions.txt"
    )
    parser.add_argument(
        "--save-actions-count",
        type=int,
        default=200,
        help="""
            The number of steps the saved action trajectory is clipped to. NOTE
            the episode must be at least this long or it will terminate with
            error.
            """,
    )
    parser.add_argument("--play-cam-res", type=int, default=512)
    parser.add_argument(
        "--skip-render-text", action="store_true", default=False
    )
    parser.add_argument(
        "--same-task",
        action="store_true",
        default=False,
        help="If true, then do not add the render camera for better visualization",
    )
    parser.add_argument(
        "--skip-task",
        action="store_true",
        default=False,
        help="If true, then do not add the render camera for better visualization",
    )
    parser.add_argument(
        "--never-end",
        action="store_true",
        default=False,
        help="If true, make the task never end due to reaching max number of steps",
    )
    parser.add_argument(
        "--add-ik",
        action="store_true",
        default=False,
        help="If true, changes arm control to IK",
    )
    parser.add_argument(
        "--gfx",
        action="store_true",
        default=False,
        help="Save a GFX replay file.",
    )
    parser.add_argument("--load-actions", type=str, default=None)
    parser.add_argument("--cfg", type=str, default=DEFAULT_CFG)
    parser.add_argument(
        "opts",
        default=None,
        nargs=argparse.REMAINDER,
        help="Modify config options from command line",
    )
    args = parser.parse_args()
    if not has_pygame() and not args.no_render:
        raise ImportError(
            "Need to install PyGame (run `pip install pygame==2.0.1`)"
        )

    config = habitat.get_config(args.cfg, args.opts)
    config.defrost()
    if not args.same_task:
        config.SIMULATOR.THIRD_RGB_SENSOR.WIDTH = args.play_cam_res
        config.SIMULATOR.THIRD_RGB_SENSOR.HEIGHT = args.play_cam_res
        config.SIMULATOR.AGENT_0.SENSORS.append("THIRD_RGB_SENSOR")
        config.SIMULATOR.DEBUG_RENDER = True
        config.TASK.COMPOSITE_SUCCESS.MUST_CALL_STOP = False
        config.TASK.REARRANGE_NAV_TO_OBJ_SUCCESS.MUST_CALL_STOP = False
        config.TASK.FORCE_TERMINATE.MAX_ACCUM_FORCE = -1.0
        config.TASK.FORCE_TERMINATE.MAX_INSTANT_FORCE = -1.0
    if args.gfx:
        config.SIMULATOR.HABITAT_SIM_V0.ENABLE_GFX_REPLAY_SAVE = True
        config.TASK.MEASUREMENTS.append("GFX_REPLAY_MEASURE")
    if args.never_end:
        config.ENVIRONMENT.MAX_EPISODE_STEPS = 0
    if args.add_ik:
        if "ARM_ACTION" not in config.TASK.ACTIONS:
            raise ValueError(
                "Action space does not have any arm control so incompatible with `--add-ik` option"
            )
        config.TASK.ACTIONS.ARM_ACTION.ARM_CONTROLLER = "ArmEEAction"
        config.SIMULATOR.IK_ARM_URDF = (
            "data/robots/hab_spot_arm/urdf/hab_spot_onlyarm.urdf"
        )
    config.freeze()

    print(DEFAULT_CFG)

    now = time.time()
    with habitat.Env(config=config) as env:
        obs = env.reset()
        before_base_pos = env.sim.robot.base_pos
        before_base_rot = env.sim.robot.base_rot
        visited_list = []
        visited_list.append(
            [vec2list(before_base_pos), float(before_base_rot)]
        )
        total_point = play_env(env, args, config, 0, visited_list, True)

    later = time.time()
    difference = int(later - now)

    print("Total nav points:", total_point)
    print("Length of visited list:", len(visited_list))
    print("Total time:", difference, "sec")
    print(DEFAULT_CFG)
    with open(
        "/Users/jimmytyyang/Documents/" + SAVE_NAME + "_visit_points.pkl", "wb"
    ) as f:
        pickle.dump([difference, total_point, visited_list], f)

    x = []
    y = []
    for point in visited_list:
        x.append(point[0][0])
        y.append(point[0][2])

    plt.scatter(x, y, c="red")
    plt.savefig("/Users/jimmytyyang/Documents/" + SAVE_NAME + "_area.png")
    import pdb

    pdb.set_trace()