# -*- coding: utf8 -*-

import argparse
from copy import deepcopy
import random
import time
import os

import matplotlib.pyplot as plt
import numpy as np

import pydrake
from pydrake.all import (
    CompliantMaterial,
    DiagramBuilder,
    PiecewisePolynomial,
    RigidBodyFrame,
    RigidBodyPlant,
    RigidBodyTree,
    RungeKutta2Integrator,
    RungeKutta3Integrator,
    Shape,
    SignalLogger,
    Simulator,
)

import kuka_controllers
import kuka_ik
import kuka_utils
import cutting_utils

if __name__ == "__main__":
    np.set_printoptions(precision=5, suppress=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("-T", "--duration",
                        type=float,
                        help="Duration to run sim.",
                        default=1000.0)
    parser.add_argument("--test",
                        action="store_true",
                        help="Help out CI by launching a meshcat server for "
                             "the duration of the test.")
    parser.add_argument("-N", "--n_objects",
                        type=int, default=2,
                        help="# of objects to spawn")
    parser.add_argument("--seed",
                        type=float, default=time.time(),
                        help="RNG seed")
    parser.add_argument("--animate_forever",
                        action="store_true",
                        help="Animates the completed sim in meshcat on loop.")

    args = parser.parse_args()
    int_seed = int(args.seed*1000. % 2**32)
    random.seed(int_seed)
    np.random.seed(int_seed)

    meshcat_server_p = None
    if args.test:
        print "Spawning"
        import subprocess
        meshcat_server_p = subprocess.Popen(["meshcat-server"])
    else:
        print "Warning: if you have not yet run meshcat-server in another " \
              "terminal, this will hang."

    # Construct the initial robot and its environment
    world_builder = kuka_utils.ExperimentWorldBuilder()

    rbt, rbt_just_kuka, q0 = world_builder.setup_initial_world(
        n_objects=args.n_objects)
    x = np.zeros(rbt.get_num_positions() + rbt.get_num_velocities())

    # Record the history of sim in a set of (state_log, rbt) slices
    # so that we can reconstruct / animate it at the end.
    sim_slices = []

    x[0:q0.shape[0]] = q0
    t = 0
    mrbv = None
    while 1:
        mrbv = kuka_utils.ReinitializableMeshcatRigidBodyVisualizer(
                rbt, draw_timestep=0.01, old_mrbv=mrbv,
                clear_vis=(mrbv is None))
        # (wait while the visualizer warms up and loads in the models)
        mrbv.draw(x)

        # Make our RBT into a plant for simulation
        rbplant = RigidBodyPlant(rbt)
        allmaterials = CompliantMaterial()
        allmaterials.set_youngs_modulus(1E8)  # default 1E9
        allmaterials.set_dissipation(0.8)     # default 0.32
        allmaterials.set_friction(0.9)        # default 0.9.
        rbplant.set_default_compliant_material(allmaterials)

        # Build up our simulation by spawning controllers and loggers
        # and connecting them to our plant.
        builder = DiagramBuilder()
        # The diagram takes ownership of all systems
        # placed into it.
        rbplant_sys = builder.AddSystem(rbplant)

        # Spawn the controller that drives the Kuka to its
        # desired posture.
        kuka_controller = builder.AddSystem(
            kuka_controllers.InstantaneousKukaController(rbt, rbplant_sys))
        builder.Connect(rbplant_sys.state_output_port(),
                        kuka_controller.robot_state_input_port)
        builder.Connect(kuka_controller.get_output_port(0),
                        rbplant_sys.get_input_port(0))

        # Same for the hand
        hand_controller = builder.AddSystem(
            kuka_controllers.HandController(rbt, rbplant_sys))
        builder.Connect(rbplant_sys.state_output_port(),
                        hand_controller.robot_state_input_port)
        builder.Connect(hand_controller.get_output_port(0),
                        rbplant_sys.get_input_port(1))

        # And the guillotine
        knife_controller = builder.AddSystem(
            kuka_controllers.GuillotineController(rbt, rbplant_sys))
        builder.Connect(rbplant_sys.state_output_port(),
                        knife_controller.robot_state_input_port)
        builder.Connect(knife_controller.get_output_port(0),
                        rbplant_sys.get_input_port(2))

        # Create a high-level state machine to guide the robot
        # motion...
        task_planner = builder.AddSystem(
            kuka_controllers.TaskPlanner(rbt, q0, world_builder))
        builder.Connect(rbplant_sys.state_output_port(),
                        task_planner.robot_state_input_port)
        builder.Connect(task_planner.hand_setpoint_output_port,
                        hand_controller.setpoint_input_port)
        builder.Connect(task_planner.kuka_setpoint_output_port,
                        kuka_controller.setpoint_input_port)
        builder.Connect(task_planner.knife_setpoint_output_port,
                        knife_controller.setpoint_input_port)

        cutting_guard = builder.AddSystem(
            cutting_utils.CuttingGuard(
                name="blade cut guard",
                rbt=rbt, rbp=rbplant,
                cutting_body_index=world_builder.guillotine_blade_index,
                cut_direction=[0., 0., -1.],
                cut_normal=[1., 0., 0.],
                min_cut_force=10.,
                cuttable_body_indices=world_builder.manipuland_body_indices,
                timestep=0.001,
                last_cut_time=t))
        builder.Connect(rbplant_sys.state_output_port(),
                        cutting_guard.state_input_port)
        builder.Connect(rbplant_sys.contact_results_output_port(),
                        cutting_guard.contact_results_input_port)

        # Hook up loggers for the robot state, the robot setpoints,
        # and the torque inputs.
        def log_output(output_port, rate):
            logger = builder.AddSystem(SignalLogger(output_port.size()))
            logger.set_publish_period(1. / rate)
            builder.Connect(output_port, logger.get_input_port(0))
            return logger
        state_log = log_output(rbplant_sys.get_output_port(0), 60.)
        kuka_control_log = log_output(
            kuka_controller.get_output_port(0), 60.)

        # Hook up the visualizer we created earlier.
        visualizer = builder.AddSystem(mrbv)
        builder.Connect(rbplant_sys.state_output_port(),
                        visualizer.get_input_port(0))

        # Done! Compile it all together and visualize it.
        diagram = builder.Build()

        # Create a simulator for it.
        simulator = Simulator(diagram)
        simulator.Initialize()
        simulator.set_target_realtime_rate(1.0)
        # Simulator time steps will be very small, so don't
        # force the rest of the system to update every single time.
        simulator.set_publish_every_time_step(False)

        # From iiwa_wsg_simulation.cc:
        # When using the default RK3 integrator, the simulation stops
        # advancing once the gripper grasps the box.  Grasping makes the
        # problem computationally stiff, which brings the default RK3
        # integrator to its knees.
        timestep = 0.00005
        integrator = RungeKutta2Integrator(diagram, timestep,
                                           simulator.get_mutable_context())
        simulator.reset_integrator(integrator)

        # The simulator simulates forward from a given Context,
        # so we adjust the simulator's initial Context to set up
        # the initial state.
        state = simulator.get_mutable_context().\
            get_mutable_continuous_state_vector()
        initial_state = np.zeros(x.shape)
        initial_state[0:x.shape[0]] = x.copy()
        state.SetFromVector(initial_state)
        simulator.get_mutable_context().set_time(t)

        # This kicks off simulation.
        rbt_new = None
        try:
            simulator.StepTo(args.duration)
        except cutting_utils.CutException as e:
            # The Cutting Guard detected a cut event.
            # Generate the new RBT and then continue the
            # simulation.
            t = simulator.get_mutable_context().get_time()
            print "Handling cut event at time %f" % t
            x = simulator.get_mutable_context().\
                get_mutable_continuous_state_vector().CopyToVector()[
                0:x.shape[0]]
            rbt_new, x = world_builder.do_cut(
                rbt, x, cut_body_index=e.cut_body_index,
                cut_pt=e.cut_pt, cut_normal=e.cut_normal)
        except StopIteration:
            print "Terminated early"
        except RuntimeError as e:
            print "Runtime Error: ", e
            print "Probably NAN in simulation. Terminating early."

        sim_slices.append((rbt, PiecewisePolynomial.FirstOrderHold(
                            #  Discard first knot, as it's repeated
                            state_log.sample_times()[1:],
                            state_log.data()[:, 1:])))
        if rbt_new:
            # Cloning one RBT into another only sort of works;
            # instead force rbt to become a reference to rbt_new,
            # and let rbt get garbage collected.
            rbt = None
            rbt = rbt_new
        else:
            break

    print("Final state: ", state.CopyToVector())
    end_time = simulator.get_mutable_context().get_time()

    if args.animate_forever:
        try:
            while (1):
                mrbv = kuka_utils.ReinitializableMeshcatRigidBodyVisualizer(
                    sim_slices[0][0], draw_timestep=0.01, clear_vis=True)
                time.sleep(1.0)
                for rbt, traj in sim_slices:
                    mrbv = kuka_utils.\
                        ReinitializableMeshcatRigidBodyVisualizer(
                            rbt, old_mrbv=mrbv, draw_timestep=0.01)
                    mrbv.animate(traj, time_scaling=1.0)
        except Exception as e:
            print "Exception during visualization: ", e

    if meshcat_server_p is not None:
        meshcat_server_p.kill()
        meshcat_server_p.wait()
