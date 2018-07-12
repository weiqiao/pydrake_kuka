# -*- coding: utf8 -*-

import numpy as np
import pydrake
from pydrake.all import (
    PiecewisePolynomial
)
from pydrake.solvers import ik

import kuka_utils


def plan_ee_configuration(rbt, q0, qseed, target_ee_pose):
    ''' Performs IK for a single point in time
        to get the Kuka's gripper to a specified
        pose in space. '''
    nq = rbt.get_num_positions()
    q_des_full = np.zeros(nq)

    controlled_joint_names = [
        "iiwa_joint_1",
        "iiwa_joint_2",
        "iiwa_joint_3",
        "iiwa_joint_4",
        "iiwa_joint_5",
        "iiwa_joint_6",
        "iiwa_joint_7"
        ]
    free_config_inds, constrained_config_inds = \
        kuka_utils.extract_position_indices(rbt, controlled_joint_names)

    # Assemble IK constraints
    constraints = []

    constraints.append(ik.MinDistanceConstraint(
        model=rbt, min_distance=0.01, active_bodies_idx=list(),
        active_group_names=set()))

    # Constrain the non-searched-over joints
    posture_constraint = ik.PostureConstraint(rbt)
    posture_constraint.setJointLimits(
        constrained_config_inds,
        q0[constrained_config_inds]-0.01, q0[constrained_config_inds]+0.01)
    constraints.append(posture_constraint)

    # Constrain the ee frame to lie on the target point
    # facing in the target orientation
    ee_frame = rbt.findFrame("iiwa_frame_ee").get_frame_index()
    constraints.append(
        ik.WorldPositionConstraint(
            rbt, ee_frame, np.zeros((3, 1)),
            target_ee_pose[0:3]-0.01, target_ee_pose[0:3]+0.01)
    )
    constraints.append(
        ik.WorldEulerConstraint(
            rbt, ee_frame,
            target_ee_pose[3:6]-0.01, target_ee_pose[3:6]+0.01)
    )

    options = ik.IKoptions(rbt)
    results = ik.InverseKin(
        rbt, q0, q0, constraints, options)
    print results.q_sol, "info %d" % results.info[0]
    return results.q_sol[0], results.info[0]


def plan_trajectory_to_posture(rbt, q0, qf, n_knots, duration,
                               start_time=0.):
    ''' Solves IK at a series of sample times (connected with a
    cubic spline) to generate a trajectory to bring the Kuka from an
    initial pose q0 to a final end effector pose in the specified
    time, using the specified number of knot points.

    Uses an intermediate pose reach_pose as an intermediate target
    to hit at the knot point closest to reach_time.

    See http://drake.mit.edu/doxygen_cxx/rigid__body__ik_8h.html
    for the "inverseKinTraj" entry. At the moment, the Python binding
    for this function uses "inverseKinTrajSimple" -- i.e., it doesn't
    return derivatives. '''
    nq = rbt.get_num_positions()
    q_des_full = np.zeros(nq)

    # Create knot points
    ts = np.linspace(0., duration, n_knots)

    controlled_joint_names = [
        "iiwa_joint_1",
        "iiwa_joint_2",
        "iiwa_joint_3",
        "iiwa_joint_4",
        "iiwa_joint_5",
        "iiwa_joint_6",
        "iiwa_joint_7",
        "left_finger_sliding_joint",
        "right_finger_sliding_joint"
        ]
    free_config_inds, constrained_config_inds = \
        kuka_utils.extract_position_indices(rbt, controlled_joint_names)

    # Assemble IK constraints
    constraints = []

    q0 = np.clip(q0, rbt.joint_limit_min, rbt.joint_limit_max)
    qf = np.clip(qf, rbt.joint_limit_min[free_config_inds],
                 rbt.joint_limit_max[free_config_inds])

    #constraints.append(ik.MinDistanceConstraint(
    #    model=rbt, min_distance=0.01, active_bodies_idx=list(),
    #    active_group_names=set()))

    # Constrain the non-searched-over joints for all time
    all_tspan = np.array([0., duration])
    posture_constraint = ik.PostureConstraint(rbt, all_tspan)
    posture_constraint.setJointLimits(
        constrained_config_inds,
        q0[constrained_config_inds]-0.01, q0[constrained_config_inds]+0.01)
    constraints.append(posture_constraint)

    # Constrain all joints to be the initial posture at the start time
    # TODO: actually freeze these, rather than letting them be searched over.
    # No point actually searching over these, and makes IK way slower
    start_tspan = np.array([0., 0.])
    posture_constraint = ik.PostureConstraint(rbt, start_tspan)
    posture_constraint.setJointLimits(
        free_config_inds,
        q0[free_config_inds]-0.01, q0[free_config_inds]+0.01)
    constraints.append(posture_constraint)

    # Constrain constrainted joints to be the final posture at the final time
    end_tspan = np.array([duration, duration])
    posture_constraint = ik.PostureConstraint(rbt, end_tspan)
    posture_constraint.setJointLimits(
        free_config_inds,
        qf[free_config_inds]-0.01, qf[free_config_inds]+0.01)
    constraints.append(posture_constraint)

    # Seed is joint-space linear interpolation of the
    # initial and final posture (a reasonable initial guess)
    q_seed = np.zeros([q0.shape[0], n_knots])
    qf_full = q0*0.
    qf_full[free_config_inds] = qf
    for i, t in enumerate(ts):
        frac = t / duration
        q_seed[:, i] = q0 * (1. - frac) + qf_full * (frac)
    # Nominal is the final posture
    q_nom = np.tile(qf_full, [1, n_knots])
    options = ik.IKoptions(rbt)
    # Set bounds on initial and final velocities
    zero_velocity = np.zeros(rbt.get_num_velocities())
    options.setqd0(zero_velocity, zero_velocity)
    options.setqdf(zero_velocity, zero_velocity)
    results = ik.InverseKinTraj(rbt, ts, q_seed, q_nom,
                                constraints, options)

    ts += start_time
    qtraj = PiecewisePolynomial.Pchip(ts, np.vstack(results.q_sol).T, True)

    print "IK returned a solution with info %d" % results.info[0]
    print "(Info 1 is good, other values are dangerous)"
    return qtraj, results.info[0]


def plan_ee_trajectory(rbt, q0, target_reach_pose,
                       target_grasp_pose, n_knots,
                       reach_time, grasp_time):
    ''' Solves IK at a series of sample times (connected with a
    cubic spline) to generate a trajectory to bring the Kuka from an
    initial pose q0 to a final end effector pose in the specified
    time, using the specified number of knot points.

    Uses an intermediate pose reach_pose as an intermediate target
    to hit at the knot point closest to reach_time.

    See http://drake.mit.edu/doxygen_cxx/rigid__body__ik_8h.html
    for the "inverseKinTraj" entry. At the moment, the Python binding
    for this function uses "inverseKinTrajSimple" -- i.e., it doesn't
    return derivatives. '''
    nq = rbt.get_num_positions()
    q_des_full = np.zeros(nq)

    # Create knot points
    ts = np.linspace(0., grasp_time, n_knots)
    # Figure out the knot just before reach time
    reach_start_index = np.argmax(ts >= reach_time) - 1

    controlled_joint_names = [
        "iiwa_joint_1",
        "iiwa_joint_2",
        "iiwa_joint_3",
        "iiwa_joint_4",
        "iiwa_joint_5",
        "iiwa_joint_6",
        "iiwa_joint_7"
        ]
    free_config_inds, constrained_config_inds = \
        kuka_utils.extract_position_indices(rbt, controlled_joint_names)

    # Assemble IK constraints
    constraints = []

    # Constrain the non-searched-over joints for all time
    all_tspan = np.array([0., grasp_time])
    posture_constraint = ik.PostureConstraint(rbt, all_tspan)
    posture_constraint.setJointLimits(
        constrained_config_inds,
        q0[constrained_config_inds]-0.01, q0[constrained_config_inds]+0.01)
    constraints.append(posture_constraint)

    # Constrain all joints to be the initial posture at the start time
    start_tspan = np.array([0., 0.])
    posture_constraint = ik.PostureConstraint(rbt, start_tspan)
    posture_constraint.setJointLimits(
        free_config_inds,
        q0[free_config_inds]-0.01, q0[free_config_inds]+0.01)
    constraints.append(posture_constraint)

    # Constrain the ee frame to lie on the target point
    # facing in the target orientation in between the
    # reach and final times
    ee_frame = rbt.findFrame("iiwa_frame_ee").get_frame_index()
    for i in range(reach_start_index, n_knots):
        this_tspan = np.array([ts[i], ts[i]])
        interp = float(i - reach_start_index) / (n_knots - reach_start_index)
        target_pose = (1.-interp)*target_reach_pose + interp*target_grasp_pose
        constraints.append(
            ik.WorldPositionConstraint(
                rbt, ee_frame, np.zeros((3, 1)),
                target_pose[0:3]-0.01, target_pose[0:3]+0.01,
                tspan=this_tspan)
        )
        constraints.append(
            ik.WorldEulerConstraint(
                rbt, ee_frame,
                target_pose[3:6]-0.05, target_pose[3:6]+0.05,
                tspan=this_tspan)
        )

    # Seed and nom are both the initial repeated for the #
    # of knot points
    q_seed = np.tile(q0, [1, n_knots])
    q_nom = np.tile(q0, [1, n_knots])
    options = ik.IKoptions(rbt)
    # Set bounds on initial and final velocities
    zero_velocity = np.zeros(rbt.get_num_velocities())
    options.setqd0(zero_velocity, zero_velocity)
    options.setqdf(zero_velocity, zero_velocity)
    results = ik.InverseKinTraj(rbt, ts, q_seed, q_nom,
                                constraints, options)

    qtraj = PiecewisePolynomial.Pchip(ts, np.vstack(results.q_sol).T, True)

    print "IK returned a solution with info %d" % results.info[0]
    print "(Info 1 is good, other values are dangerous)"
    return qtraj, results.info[0]
