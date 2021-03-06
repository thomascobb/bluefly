import asyncio
from dataclasses import dataclass
from typing import Dict, Sequence, Set, Tuple

import numpy as np
from scanpointgenerator import Point

from bluefly.motor import MotorDevice, MotorRecord

from .core import (
    HasSignals,
    NotConnectedError,
    RemainingPoints,
    SignalR,
    SignalW,
    SignalX,
    signal_sources,
)

# 9 axes in a PMAC co-ordinate system
CS_AXES = "abcuvwxyz"


@signal_sources(demands={x: f"demand_{x}" for x in CS_AXES})
class PMACCoord(HasSignals):
    port: SignalR[str]
    demands: SignalW[Dict[str, float]]
    move_time: SignalW[float]
    defer_moves: SignalW[bool]


@signal_sources(
    positions={x: f"position_{x}" for x in CS_AXES},
    use={x: f"use_{x}" for x in CS_AXES},
)
class PMACTrajectory(HasSignals):
    times: SignalW[Sequence[float]]
    velocity_modes: SignalW[Sequence[float]]
    user_programs: SignalW[Sequence[int]]
    positions: SignalW[Dict[str, Sequence[float]]]
    use: SignalW[Dict[str, bool]]
    cs: SignalW[str]
    build: SignalX
    build_message: SignalR[str]
    build_status: SignalR[str]
    points_to_build: SignalW[int]
    append: SignalX
    append_message: SignalR[str]
    append_status: SignalR[str]
    execute: SignalX
    execute_message: SignalR[str]
    execute_status: SignalR[str]
    points_scanned: SignalR[int]
    abort: SignalX
    program_version: SignalR[float]


class PMAC:
    def __init__(self, pv_prefix: str):
        self.pv_prefix = pv_prefix
        self.cs_list = [PMACCoord(f"{pv_prefix}CS{i+1}") for i in range(16)]
        self.traj = PMACTrajectory(f"{pv_prefix}TRAJ")


class PMACCompoundMotor(MotorRecord):
    input_link: SignalR[str]


class PMACRawMotor(MotorRecord):
    cs_axis: SignalR[str]
    cs_port: SignalR[str]


BATCH_SIZE = 100


@dataclass
class TrajectoryBatch:
    times: Sequence[float]
    velocity_modes: Sequence[float]
    user_programs: Sequence[int]
    positions: Dict[str, Sequence[float]]


@dataclass
class TrajectoryTracker:
    points: RemainingPoints
    cs_axes: Dict[str, str]

    def ready_for_next_batch(self, step: int):
        return self.points.remaining and self.points.completed < step + BATCH_SIZE

    def get_next_batch(self) -> TrajectoryBatch:
        points = self.points.get_points(BATCH_SIZE)
        return TrajectoryBatch(
            times=points.duration,
            velocity_modes=np.zeros(len(points)),
            user_programs=np.zeros(len(points)),
            positions={self.cs_axes[k]: v for k, v in points.positions.items()},
        )


async def get_cs(motors: Sequence[MotorDevice]) -> Tuple[str, Dict[str, str]]:
    cs_ports: Set[str] = set()
    cs_axes: Dict[str, str] = {}
    for sm in motors:
        assert sm.name
        motor = sm.motor
        if isinstance(motor, PMACRawMotor):
            cs_ports.add(await motor.cs_port.get())
            cs_axes[sm.name] = (await motor.cs_axis.get()).lower()
        else:
            raise NotImplementedError("Not handled PMAC compound motor yet")
    cs_ports_list = sorted(cs_ports)
    assert len(cs_ports_list) == 1, f"Expected one CS, got {cs_ports_list}"
    return cs_ports_list[0], cs_axes


async def move_to_start(
    pmac: PMAC, motors: Sequence[MotorDevice], first_point: Point,
):
    cs_port, cs_axes = await get_cs(motors)
    for cs in pmac.cs_list:
        try:
            if await cs.port.get() == cs_port:
                break
        except NotConnectedError:
            # Some CS are not implemented for all PMACs
            continue
    else:
        raise ValueError(f"No CS given for {cs_port!r}")
    await cs.defer_moves.set(True)
    # insert real axis run-up calcs here
    demands = {cs_axes[axis]: value for axis, value in first_point.positions.items()}
    await cs.demands.set(demands)
    await cs.defer_moves.set(False)


async def check_traj_success(status, message):
    if not await status.get() == "Success":
        raise ValueError(await message.get())


async def build_initial_trajectory(
    pmac: PMAC, motors: Sequence[MotorDevice], points: RemainingPoints,
) -> TrajectoryTracker:
    cs_port, cs_axes = await get_cs(motors)
    traj = pmac.traj
    await traj.cs.set(cs_port)
    tracker = TrajectoryTracker(points, cs_axes)
    batch = tracker.get_next_batch()
    await asyncio.gather(
        traj.times.set(batch.times),
        traj.user_programs.set(batch.user_programs),
        traj.velocity_modes.set(batch.velocity_modes),
        traj.positions.set(batch.positions),
        traj.points_to_build.set(len(batch.times)),
        traj.use.set({x: x in batch.positions for x in CS_AXES}),
    )
    await traj.build()
    await check_traj_success(traj.build_status, traj.build_message)
    return tracker


async def keep_filling_trajectory(pmac: PMAC, tracker: TrajectoryTracker):
    traj = pmac.traj
    task = asyncio.create_task(traj.execute())
    async for step in traj.points_scanned.observe():
        if task.done():
            break
        if tracker.ready_for_next_batch(step):
            # Push a new batch of points
            batch = tracker.get_next_batch()
            await asyncio.gather(
                traj.times.set(batch.times),
                traj.user_programs.set(batch.user_programs),
                traj.velocity_modes.set(batch.velocity_modes),
                traj.positions.set(batch.positions),
                traj.points_to_build.set(len(batch.times)),
            )
            await traj.append()
            await check_traj_success(traj.append_status, traj.append_message)
    await check_traj_success(traj.execute_status, traj.execute_message)


async def stop_trajectory(pmac: PMAC):
    await pmac.traj.abort()
