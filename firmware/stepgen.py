

# Imports for creating a LiteX/Migen module
from turtle import position, speed
from litex.soc.interconnect.csr import *
from migen import *
from litex.soc.integration.doc import AutoDoc, ModuleDoc


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

    def __init__(self, pick_off) -> None:

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

        """
        )

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

        # Output parameters
        self.step = Signal()
        self.step_prev = Signal()
        self.dir = Signal()

        # Main parameters for position, speed and acceleration
        self.enable = Signal()
        self.position = Signal(64)
        self.speed = Signal(32)
        self.speed_target = Signal(32)
        self.max_acceleration = Signal(32)

        # Optionally, use a different clock domain
        sync = self.sync

        # Determine the next speed, while taking into account acceleration limits if
        # applied. The speed is not updated when the direction has changed and we are
        # still waiting for the dir_setup to time out.
        sync += If(
            ~self.hold_dds,
            If(
                ~self.max_acceleration,
                # Case: no maximum acceleration defined, directly apply the requested speed
                self.speed.eq(self.speed_target)
            ).Else(
                # Case: obey the maximum acceleration / deceleration
                If(
                    # Accelerate, difference between actual speed and target speed is too
                    # large to bridge within one clock-cycle
                    self.speed_target > (self.speed + self.max_acceleration),
                    self.speed.eq(self.speed + self.max_acceleration)
                ).Elif(
                    # Deceerate, difference between actual speed and target speed is too
                    # large to bridge within one clock-cycle
                    self.speed_target < (self.speed - self.max_acceleration),
                    self.speed.eq(self.speed - self.max_acceleration)
                ).Else(
                    # Small difference between speed and target speed, gap can be bridged within
                    # one clock cycle.
                    self.speed.eq(self.speed_target)
                )
            )
        )

        # Update the position
        sync += If(
            # Check whether the system is enabled and we are not waiting for the dir_setup
            self.enable & ~self.hold_dds,
            If(
                # Check the MSB of the speed, this indicates the direction of the travel
                self.speed[31],
                self.position.eq(self.position + self.speed[:31])
            ).Else(
                self.position.eq(self.position - self.speed[:31])
            )
        )

        # Translate the position to steps by looking at the n'th bit (pick-off)
        sync += If(
            self.position[pick_off] != self.step_prev,
            # The relevant bit has toggled, make a step to the next position by
            # resetting the counters
            self.step_prev.eq(self.position[pick_off]),
            self.steplen_counter.counter.eq(self.steplen),
            self.dir_hold_counter.counter.eq(self.steplen + self.dir_hold_time),
            self.dir_setup_counter.counter.eq(self.steplen + self.dir_hold_time + self.dir_setup_time)
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
            self.dir != (self.speed[31]),
            # Enable the Hold DDS, but wait with changing the dir pin until the 
            # dir_hold_counter has been elapsed
            self.hold_dds.eq(1),
            If(
                self.dir_hold_counter.counter == 0,
                self.dir.eq(self.speed[31])
            ) 
        )

        # Create the outputs
        self.ios = {self.step, self.dir}


if __name__ == "__main__":
    from migen import *
    from migen.fhdl import *

    def test_stepgen(stepgen):
        i = 0
        # Setup the stepgen
        yield(stepgen.enable.eq(1))
        yield(stepgen.speed_target.eq(int(2**28 / 128)))
        yield(stepgen.steplen.eq(16))
        yield(stepgen.dir_hold_time.eq(16))
        yield(stepgen.dir_setup_time.eq(32))

        while(1):
            if i == 390:
                yield(stepgen.speed_target.eq(int(2**28 / 128) + 2**31))
            position = (yield stepgen.position[:32])
            step = (yield stepgen.step)
            dir = (yield stepgen.dir)
            counter = (yield stepgen.dir_hold_counter.counter)
            print("position = %d @step %d @dir %d @dir_counter %d@clk %d"%(position, step, dir, counter, i))
            yield
            i+=1
            if i > 1024:
                break

    stepgen = StepgenModule(pick_off=28)
    print("\nRunning Sim...\n")
    # print(verilog.convert(stepgen, stepgen.ios, "pre_scaler"))
    run_simulation(stepgen, test_stepgen(stepgen))
