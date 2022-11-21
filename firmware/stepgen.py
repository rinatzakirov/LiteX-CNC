# Imports for creating a json-definition
from typing import Iterable, List
from pydantic import BaseModel, Field

# Imports for creating a LiteX/Migen module
from litex.soc.interconnect.csr import *
from migen import *
from migen.fhdl.structure import Cat, Constant
from litex.soc.integration.soc import SoC
from litex.soc.integration.doc import AutoDoc, ModuleDoc
from litex.build.generic_platform import *


class StepgenConfig(BaseModel):
    step_pin: str = Field(
        description="The pin on the FPGA-card for the step signal."
    )
    dir_pin: str = Field(
        None,
        description="The pin on the FPGA-card for the dir signal."
    )
    name: str = Field(
        None,
        description="The name of the stepgen as used in LinuxCNC HAL-file (optional). "
    )
    soft_stop: bool = Field(
        False,
        description="When False, the stepgen will directly stop when the stepgen is "
        "disabled. When True, the stepgen will stop the machine with respect to the "
        "acceleration limits and then be disabled. Default value: False."
    )
    io_standard: str = Field(
        "LVCMOS33",
        description="The IO Standard (voltage) to use for the pins."
    )
    pseudo_diff: bool = Field(
        False,
        description="When True, there are two pins in definition separated by / that describe a pair in cases "
        "where driver is not really a differential driver, but just two separately driven pins"
    )


class StepgenCounter(Module, AutoDoc):

    def __init__(self) -> None:

        self.intro = ModuleDoc("""
        Simple counter which counts down as soon as the Signal
        `counter` has a value larger then 0. Designed for the
        several timing components of the StepgenModule.
        """)

        # Create a 32 bit counter which counts down
        self.counter = Signal(32)
        self.sync += If(
            self.counter > 0,
            self.counter.eq(self.counter - 1)
        )


class StepgenModule(Module, AutoDoc):
    pads_layout = [("step", 1), ("dir", 1)]

    def __init__(self, pads, pick_off, soft_stop) -> None:
        """
        
        NOTE: pickoff should be a three-tuple. A different pick-off for position, speed
        and acceleration is supported. When pick-off is a integer, all the pick offs will
        be the same.
        """

        self.intro = ModuleDoc("""

        Timing parameters:
        There are five timing parameters which control the output waveform.
        No step type uses all five, and only those which will be used are
        exported to HAL.  The values of these parameters are in nano-seconds,
        so no recalculation is needed when changing thread periods.  In
        the timing diagrams that follow, they are identfied by the
        following numbers:
        (1): 'steplen' = length of the step pulse
        (2): 'stepspace' = minimum space between step pulses, space is dependent
        on the commanded speed. The check whether the minimum step space is obeyed
        is done in the driver
        (3): 'dirhold_time' = minimum delay after a step pulse before a direction
        change - may be longer
        (4): 'dir_setup_time' = minimum delay after a direction change and before
        the next step - may be longer

                   _____         _____               _____
        STEP  ____/     \_______/     \_____________/     \______
                  |     |       |     |             |     |
        Time      |-(1)-|--(2)--|-(1)-|--(3)--|-(4)-|-(1)-|
                                              |__________________
        DIR   ________________________________/

        Improvements on LinuxCNC stepgen.c:
        - When the machine is at rest and starts a commanded move, it can be moved
          the opposite way. This means that the dir-signal is toggled and thus a wait
          time is applied before the step-pin is toggled.
        - When changing direction between two steps, it is not necessary to wait. That's
          why there are signals for DDS (1+3+4) and for wait. Only when a step is
          commanded during the DDS period, the stepgen is temporarily paused by setting
          the wait-Signal HIGH.
        """
        )
        # Require to test working with Verilog, basically creates extra signals not
        # connected to any pads.
        if pads is None:
            pads = Record(self.pads_layout)
        self.pads = pads

        # Store the pick-off (to prevent magic numbers later in the code)
        if isinstance(pick_off, int):
            self.pick_off_pos = pick_off
            self.pick_off_vel = pick_off
            self.pick_off_acc = pick_off
        elif isinstance(pick_off, Iterable) and not isinstance(pick_off, str):
            if len(pick_off) <  3:
                raise ValueError(f"Not enough values for `pick_off` ({len(pick_off)}), minimum length is 3.")
            self.pick_off_pos = pick_off[0]
            self.pick_off_vel = max(self.pick_off_pos, pick_off[1])
            self.pick_off_acc = max(self.pick_off_vel, pick_off[2])
        else:
            raise ValueError("`pick_off` must be either a list of pick_offs or a single integer value." )

        # Calculate constants based on the pick-off
        # - speed_reset_val: 0x8000_0000 in case of 32-bit variable, otherwise increase to set the sign bit
        self.speed_reset_val = (0x8000_0000 << (self.pick_off_acc - self.pick_off_vel)) 


        # Values which determine the spacing of the step. These
        # are used to reset the counters.
        # - signals
        self.steplen = Signal(32)
        self.dir_hold_time = Signal(32)
        self.dir_setup_time = Signal(32)
        # - counters
        self.steplen_counter = StepgenCounter()
        self.dir_hold_counter = StepgenCounter()
        self.dir_setup_counter = StepgenCounter()
        self.submodules += [
            self.steplen_counter,
            self.dir_hold_counter,
            self.dir_setup_counter
        ]
        self.hold_dds = Signal()
        self.wait = Signal()
        self.reset = Signal()

        # Output parameters
        self.step = Signal()
        self.step_prev = Signal()
        self.dir = Signal(reset=True)

        # Main parameters for position, speed and acceleration
        self.enable = Signal()
        self.position = Signal(64 + (self.pick_off_vel - self.pick_off_pos))
        self.speed = Signal(
            32 + (self.pick_off_acc - self.pick_off_vel),
            reset=self.speed_reset_val
        )
        self.speed_target = Signal(
            32 + (self.pick_off_acc - self.pick_off_vel),
            reset=self.speed_reset_val
        )
        self.speed_target2 = Signal(
            32 + (self.pick_off_acc - self.pick_off_vel),
            reset=self.speed_reset_val
        )
        self.max_acceleration = Signal(32)
        self.max_acceleration2 = Signal(32)
        self.apply_time2 = Signal(64)

        # Link step and dir
        self.comb += [
            pads.step.eq(self.step),
            pads.dir.eq(self.dir),
        ]

        # Optionally, use a different clock domain
        sync = self.sync

        # Determine the next speed, while taking into account acceleration limits if
        # applied. The speed is not updated when the direction has changed and we are
        # still waiting for the dir_setup to time out.
        sync += If(
            ~self.reset & ~self.wait,
            # When the machine is not enabled, the speed is clamped to 0. This results in a
            # deceleration when the machine is disabled while the machine is running,
            # preventing possible damage.
            If(
                ~self.enable,
                self.speed_target.eq(self.speed_reset_val)
            ),
            If(
                self.max_acceleration == 0,
                # Case: no maximum acceleration defined, directly apply the requested speed
                self.speed.eq(self.speed_target)
            ).Else(
                # Case: obey the maximum acceleration / deceleration
                If(
                    # Accelerate, difference between actual speed and target speed is too
                    # large to bridge within one clock-cycle
                    self.speed_target > (self.speed + self.max_acceleration),
                    # The counters are again a fixed point arithmetric. Every loop we keep
                    # the fraction and add the integer part to the speed. The fraction is
                    # used as a starting point for the next loop.
                    self.speed.eq(self.speed + self.max_acceleration),
                ).Elif(
                    # Decelerate, difference between actual speed and target speed is too
                    # large to bridge within one clock-cycle
                    self.speed_target < (self.speed - self.max_acceleration),
                    # The counters are again a fixed point arithmetric. Every loop we keep
                    # the fraction and add the integer part to the speed. However, we have
                    # keep in mind we are subtracting now every loop
                    self.speed.eq(self.speed - self.max_acceleration)
                ).Else(
                    # Small difference between speed and target speed, gap can be bridged within
                    # one clock cycle.
                    self.speed.eq(self.speed_target)
                )
            )
        )

        # Reset algorithm.
        # NOTE: RESETTING the stepgen will not adhere the speed limit and will bring the stepgen
        # to an abrupt standstill
        sync += If(
            self.reset,
            # Prevent updating MMIO registers to prevent restart
            self.apply_time2.eq(2^64-1),
            # Reset speed and position to 0
            self.speed_target.eq(self.speed_reset_val),
            self.speed_target2.eq(self.speed_reset_val),
            self.speed.eq(self.speed_reset_val),
            self.max_acceleration.eq(0),
            self.max_acceleration2.eq(0),
            self.position.eq(0),
        )

        # Update the position
        if soft_stop:
            sync += If(
                # Only check we are not waiting for the dir_setup. When the system is disabled, the
                # speed is set to 0 (with respect to acceleration limits) and the machine will be
                # stopped when disabled.
                ~self.reset & ~self.wait,
                self.position.eq(self.position + self.speed[(self.pick_off_acc - self.pick_off_vel):] - 0x8000_0000)
            )
        else:
            sync += If(
                # Check whether the system is enabled and we are not waiting for the dir_setup
                ~self.reset & self.enable & ~self.wait,
                self.position.eq(self.position + self.speed[(self.pick_off_acc - self.pick_off_vel):] - 0x8000_0000)
            )

        # Translate the position to steps by looking at the n'th bit (pick-off)
        # NOTE: to be able to simply add the velocity to the position for every timestep, the position
        # registered is widened from the default 64-buit width to 64-bit + difference in pick-off for
        # position and velocity. This meands that the bit we have to watch is also shifted by the
        # same amount. This means that although we are watching the position, we have to use the pick-off
        # for velocity
        sync += If(
            self.position[self.pick_off_vel] != self.step_prev,
            # Corner-case: The machine is at rest and starts to move in the opposite
            # direction. Wait with stepping the machine until the dir setup time has
            # passed.
            If(
                ~self.hold_dds,
                # The relevant bit has toggled, make a step to the next position by
                # resetting the counters
                self.step_prev.eq(self.position[self.pick_off_vel]),
                self.steplen_counter.counter.eq(self.steplen),
                self.dir_hold_counter.counter.eq(self.steplen + self.dir_hold_time),
                self.dir_setup_counter.counter.eq(self.steplen + self.dir_hold_time + self.dir_setup_time),
                self.wait.eq(False)
            ).Else(
                self.wait.eq(True)
            )
        )
        # Reset the DDS flag when dir_setup_counter has lapsed
        sync += If(
            self.dir_setup_counter.counter == 0,
            self.hold_dds.eq(0)
        )

        # Convert the parameters to output of step and dir
        # - step
        sync += If(
            self.steplen_counter.counter > 0,
            self.step.eq(1)
        ).Else(
            self.step.eq(0)
        )
        # - dir
        sync += If(
            self.dir != (self.speed[32 + (self.pick_off_acc - self.pick_off_vel) - 1]),
            # Enable the Hold DDS, but wait with changing the dir pin until the
            # dir_hold_counter has been elapsed
            self.hold_dds.eq(1),
            # Corner-case: The machine is at rest and starts to move in the opposite
            # direction. In this case the dir pin is toggled, while a step can follow
            # suite. We wait in this case the minimum dir_setup_time
            If(
                self.dir_setup_counter.counter == 0,
                self.dir_setup_counter.counter.eq(self.dir_setup_time)
            ),
            If(
                self.dir_hold_counter.counter == 0,
                self.dir.eq(self.speed[32 + (self.pick_off_acc - self.pick_off_vel) - 1])
            )
        )

        # Create the outputs
        self.ios = {self.step, self.dir}

    @classmethod
    def add_mmio_config_registers(cls, mmio, config: List[StepgenConfig]):
        """
        Adds the configuration registers to the MMIO. The configuration registers
        contain information on the the timings of ALL stepgen.

        TODO: in the next iteration of the stepgen timing configs should be for each
        stepgen individually.
        """
        mmio.stepgen_steplen = CSRStorage(
            size=32,
            name=f'stepgen_steplen',
            description=f'The length of the step pulse in clock cycles',
            write_from_dev=False
        )
        mmio.stepgen_dir_hold_time = CSRStorage(
            size=32,
            name=f'stepgen_dir_hold_time',
            description=f'The minimum delay (in clock cycles) after a step pulse before '
            'a direction change - may be longer',
            write_from_dev=False
        )
        mmio.stepgen_dir_setup_time = CSRStorage(
            size=32,
            name=f'stepgen_dir_setup_time',
            description=f'The minimum delay (in clock cycles) after a direction change '
            'and before the next step - may be longer',
            write_from_dev=False
        )

    @classmethod
    def add_mmio_read_registers(cls, mmio, config: List[StepgenConfig]):
        """
        Adds the status registers to the MMIO.

        NOTE: Status registers are meant to be read by LinuxCNC and contain
        the current status of the stepgen.
        """
        # Don't create the registers when the config is empty (no stepgens
        # defined in this case)
        if not config:
            return

        for index, _ in enumerate(config):
            setattr(
                mmio,
                f'stepgen_{index}_position',
                CSRStatus(
                    size=64,
                    name=f'stepgen_{index}_position',
                    description=f'stepgen_{index}_position',
                )
            )
            setattr(
                mmio,
                f'stepgen_{index}_speed',
                CSRStatus(
                    size=32,
                    description=f'stepgen_{index}_speed',
                    name=f'stepgen_{index}_speed'
                )
            )
            setattr(
                mmio,
                f'stepgen_{index}_apply_time2',
                CSRStatus(
                    size=64,
                    description=f'stepgen_{index}_apply_time2',
                    name=f'stepgen_{index}_apply_time2'
                )
            )

    @classmethod
    def add_mmio_write_registers(cls, mmio, config: List[StepgenConfig]):
        """
        Adds the storage registers to the MMIO.

        NOTE: Storage registers are meant to be written by LinuxCNC and contain
        the flags and configuration for the module.
        """
        # Don't create the registers when the config is empty (no encoders
        # defined in this case)
        if not config:
            return
        
        # General data - equal for each stepgen
        mmio.stepgen_apply_time = CSRStorage(
            size=64,
            name=f'stepgen_apply_time',
            description=f'The time at which the current settings (as stored in stepgen_#_speed_target '
            'and stepgen_#_max_acceleration will be applied and thus a new segment will be started.',
            write_from_dev=True
        )

        # Speed and acceleration settings for the next movement segment
        for index, _ in enumerate(config):
            setattr(
                mmio,
                f'stepgen_{index}_speed_target1',
                CSRStorage(
                    size=32,
                    reset=0x80000000,  # Very important, as this is threated as 0
                    name=f'stepgen_{index}_speed_target1',
                    description=f'The target speed for stepper {index}.',
                    write_from_dev=False
                )
            )
            setattr(
                mmio,
                f'stepgen_{index}_max_acceleration1',
                CSRStorage(
                    size=32,
                    name=f'stepgen_{index}_max_acceleration1',
                    description=f'The maximum acceleration for stepper {index}. The storage contains a '
                    'fixed point value, with 16 bits before and 16 bits after the point. Each '
                    'clock cycle, this value will be added or subtracted from the stepgen speed '
                    'until the target speed is acquired.',
                    write_from_dev=False
                )
            )
            setattr(
                mmio,
                f'stepgen_{index}_part1_cycles',
                CSRStorage(
                    size=32,
                    name=f'stepgen_{index}_part1_cycles',
                    description=f'The number of cycles, starting from the generic apply time, '
                    'during which the first combination of speed_target and maximum acceleration '
                    'is applied.',
                    write_from_dev=False
                )
            )
            setattr(
                mmio,
                f'stepgen_{index}_speed_target2',
                CSRStorage(
                    size=32,
                    reset=0x80000000,  # Very important, as this is threated as 0
                    name=f'stepgen_{index}_speed_target2',
                    description=f'The target speed for stepper {index}.',
                    write_from_dev=False
                )
            )
            setattr(
                mmio,
                f'stepgen_{index}_max_acceleration2',
                CSRStorage(
                    size=32,
                    name=f'stepgen_{index}_max_acceleration2',
                    description=f'The maximum acceleration for stepper {index}. The storage contains a '
                    'fixed point value, with 16 bits before and 16 bits after the point. Each '
                    'clock cycle, this value will be added or subtracted from the stepgen speed '
                    'until the target speed is acquired.',
                    write_from_dev=False
                )
            )


    @classmethod
    def create_from_config(cls, soc: SoC, watchdog, config: List[StepgenConfig]):
        """
        Adds the module as defined in the configuration to the SoC.

        NOTE: the configuration must be a list and should contain all the module at
        once. Otherwise naming conflicts will occur.
        """
        # Don't create the module when the config is empty (no stepgens 
        # defined in this case)
        if not config:
            return

        for index, stepgen_config in enumerate(config):
            if stepgen_config.pseudo_diff:
                io_standards = stepgen_config.io_standard.split("/")
                if len(io_standards) == 1:
                    io_standards *= 2
                soc.platform.add_extension([
                    ("stepgen", index,
                        Subsignal("step_p", Pins(stepgen_config.step_pin.split("/")[0]), IOStandard(io_standards[0])),
                        Subsignal("step_n", Pins(stepgen_config.step_pin.split("/")[1]), IOStandard(io_standards[1])),
                        Subsignal("dir_p" , Pins(stepgen_config.dir_pin .split("/")[0]), IOStandard(io_standards[0])),
                        Subsignal("dir_n" , Pins(stepgen_config.dir_pin .split("/")[1]), IOStandard(io_standards[1]))
                    )
                ])
            else:
                soc.platform.add_extension([
                    ("stepgen", index,
                        Subsignal("step", Pins(stepgen_config.step_pin), IOStandard(stepgen_config.io_standard)),
                        Subsignal("dir", Pins(stepgen_config.dir_pin), IOStandard(stepgen_config.io_standard))
                    )
                ])
            # Create the stepgen and add to the system
            stepgen = cls(
                pads=None if stepgen_config.pseudo_diff else soc.platform.request('stepgen', index),
                pick_off=(32, 40, 48),
                soft_stop=stepgen_config.soft_stop
            )
            soc.submodules += stepgen
            if stepgen_config.pseudo_diff:
                pads=soc.platform.request('stepgen', index)
                soc.comb += [
                    pads.step_p.eq( stepgen.pads.step),
                    pads.step_n.eq(~stepgen.pads.step),
                    pads.dir_p .eq( stepgen.pads.dir ),
                    pads.dir_n .eq(~stepgen.pads.dir ),
                ]
            # Connect all the memory
            soc.comb += [
                # Data from MMIO to stepgen
                stepgen.reset.eq(soc.MMIO_inst.reset.storage),
                stepgen.enable.eq(~watchdog.has_bitten),
                stepgen.steplen.eq(soc.MMIO_inst.stepgen_steplen.storage),
                stepgen.dir_hold_time.eq(soc.MMIO_inst.stepgen_dir_hold_time.storage),
                stepgen.dir_setup_time.eq(soc.MMIO_inst.stepgen_dir_setup_time.storage),
            ]
            soc.sync += [
                # Position and feedback from stepgen to MMIO
                getattr(soc.MMIO_inst, f'stepgen_{index}_position').status.eq(stepgen.position[(stepgen.pick_off_vel - stepgen.pick_off_pos):]),
                getattr(soc.MMIO_inst, f'stepgen_{index}_speed').status.eq(stepgen.speed[(stepgen.pick_off_acc - stepgen.pick_off_vel):]),
                getattr(soc.MMIO_inst, f'stepgen_{index}_apply_time2').status.eq(stepgen.apply_time2),
            ]
            # Add speed target and the max acceleration in the protected sync
            soc.sync += [
                If(
                    soc.MMIO_inst.wall_clock.status >= soc.MMIO_inst.stepgen_apply_time.storage,
                    stepgen.apply_time2.eq(soc.MMIO_inst.stepgen_apply_time.storage + getattr(soc.MMIO_inst, f'stepgen_{index}_part1_cycles').storage),
                    If(
                        soc.MMIO_inst.wall_clock.status < stepgen.apply_time2,
                        stepgen.speed_target.eq(Cat(Constant(0, bits_sign=(stepgen.pick_off_acc - stepgen.pick_off_vel)), getattr(soc.MMIO_inst, f'stepgen_{index}_speed_target1').storage)),
                        stepgen.max_acceleration.eq(getattr(soc.MMIO_inst, f'stepgen_{index}_max_acceleration1').storage),
                        stepgen.speed_target2.eq(Cat(Constant(0, bits_sign=(stepgen.pick_off_acc - stepgen.pick_off_vel)), getattr(soc.MMIO_inst, f'stepgen_{index}_speed_target2').storage)),
                        stepgen.max_acceleration2.eq(getattr(soc.MMIO_inst, f'stepgen_{index}_max_acceleration2').storage)
                    )
                ),
                If(
                    soc.MMIO_inst.wall_clock.status >= stepgen.apply_time2,
                    stepgen.speed_target.eq(stepgen.speed_target2),
                    stepgen.max_acceleration.eq(stepgen.max_acceleration2)
                )
            ]
            # Add reset logic to stop the motion after reboot of LinuxCNC
            soc.sync += [
                soc.MMIO_inst.stepgen_apply_time.we.eq(0),
                If(
                    soc.MMIO_inst.reset.storage,
                    soc.MMIO_inst.stepgen_apply_time.dat_w.eq(0x80000000),
                    soc.MMIO_inst.stepgen_apply_time.we.eq(1)
                )
            ]


if __name__ == "__main__":
    from migen import *
    from migen.fhdl import *

    def test_stepgen(stepgen):
        i = 0
        # Setup the stepgen
        yield(stepgen.enable.eq(1))
        # yield(stepgen.speed_target.eq(0x80000000 - int(2**28 / 128)))
        yield(stepgen.speed.eq(0x8000_0000_0000 + (0 << 16)))
        yield(stepgen.speed_test.eq(0x8000_0000 + (0x53E)))
        yield(stepgen.max_acceleration.eq(0x36F))
        yield(stepgen.steplen.eq(16))
        yield(stepgen.dir_hold_time.eq(16))
        yield(stepgen.dir_setup_time.eq(32))
        speed_prev=0

        while(1):
            # if i == 390:
            #     yield(stepgen.speed_target.eq(0x80000000 + int(2**28 / 128)))
            position = (yield stepgen.position)
            step = (yield stepgen.step)
            dir = (yield stepgen.dir)
            speed = (yield stepgen.speed_reported - 0x8000_0000)
            counter = (yield stepgen.dir_hold_counter.counter)
            if speed != speed_prev:
                print("speed = %d, position = %d @step %d @dir %d @dir_counter %d @clk %d"%(speed, position, step, dir, counter, i))
                speed_prev = speed
            yield
            i+=1
            if i > 100000:
                break

    stepgen = StepgenModule(pads=None, pick_off=32, soft_stop=True)
    print("\nRunning Sim...\n")
    # print(verilog.convert(stepgen, stepgen.ios, "pre_scaler"))
    run_simulation(stepgen, test_stepgen(stepgen))
