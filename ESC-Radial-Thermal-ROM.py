#!/usr/bin/env python3
"""
=============================================================================
External Short-Circuit (ESC) Simulation: Baseline vs. Reduced Order Model (ROM)
=============================================================================

Reference:
Addressing the limitations of lumped thermal modelling in predicting internal
thermal runaway triggers during external short-circuit events (e.g. Zhou et al., 2026).

Code Structure:
1. Baseline Model: Executes the standard PyBaMM lumped thermal model to establish
   the conventional volume-averaged temperature baseline.
2. ROM Model: Injects a mathematically rigorous Two-Node (Core/Surface) radial
   thermal model via post-build ODE hijacking. This architecture guarantees 100%
   energy conservation whilst establishing bidirectional multiphysics coupling
   (the core hotspot temperature drives the Arrhenius electrochemical kinetics).
3. Benchmarking: Generates a comparative visualisation demonstrating the severity
   of internal thermal gradients concealed by standard lumped models.
=============================================================================
"""

import pybamm
import matplotlib.pyplot as plt

# =====================================================================
# 0. Common Setup (Shared Experiment & Mesh)
# =====================================================================
initial_temperature_K = 278.15  # 5 °C
initial_soc = 0.10              # 10% SOC

experiment = pybamm.Experiment([
    "Hold at 1.75 V for 14 seconds (0.1 second period)"
])

var_pts = {
    "x_n": 20, "x_s": 20, "x_p": 20,
    "r_n": 60, "r_p": 60
}

print("======================================================")
print("[1/2] RUNNING BASELINE: Standard Lumped Thermal Model")
print("======================================================")

# =====================================================================
# 1. Baseline Model (Conventional Volume-Averaged Approach)
# =====================================================================
options_base = {"thermal": "lumped"}
model_base = pybamm.lithium_ion.DFN(options_base)
param_base = pybamm.ParameterValues("OKane2022")

param_base.update({
    "Lower voltage cut-off [V]": 0.0,
    "Initial temperature [K]": initial_temperature_K,
    "Ambient temperature [K]": initial_temperature_K
})

solver_base = pybamm.CasadiSolver(mode="safe", dt_max=1e-6, rtol=1e-6, atol=1e-6)
sim_base = pybamm.Simulation(
    model=model_base,
    experiment=experiment,
    parameter_values=param_base,
    solver=solver_base,
    var_pts=var_pts
)

solution_base = sim_base.solve(initial_soc=initial_soc)

# Extract Baseline Data
t_base = solution_base["Time [s]"].entries
temp_base_C = solution_base["Volume-averaged cell temperature [K]"].entries - 273.15
print("[INFO] Baseline computation successfully completed.\n")


print("======================================================")
print("[2/2] RUNNING ROM: Two-Way Coupled Radial Model")
print("======================================================")

# =====================================================================
# 2. ROM Model (Post-Build ODE Hijacking)
# =====================================================================
options_rom = {"thermal": "lumped"}
model_rom = pybamm.lithium_ion.DFN(options_rom)
param_rom = pybamm.ParameterValues("OKane2022")

param_rom.update({
    "Lower voltage cut-off [V]": 0.0,
    "Initial temperature [K]": initial_temperature_K,
    "Ambient temperature [K]": initial_temperature_K,
    "Effective volumetric heat capacity [J.m-3.K-1]": 2.2e6
}, check_already_exists=False)

# Extract total volumetric heat generation calculated by the electrochemistry
Q_gen_rom = model_rom.variables["Volume-averaged total heating [W.m-3]"]

T_pybamm_native_rom = None
for var in model_rom.rhs.keys():
    if var.name == "Volume-averaged cell temperature [K]":
        T_pybamm_native_rom = var
        break

if T_pybamm_native_rom is None:
    raise ValueError("Native temperature variable not found!")

# This ensures that the severe core temperature governs the Arrhenius equations
# across the entire cell, establishing a worst-case Two-Way Coupled ROM.
T_core_rom = T_pybamm_native_rom
T_surf_rom = pybamm.Variable("Custom Surface temperature [K] ROM")

rho_cp_rom = pybamm.Parameter("Effective volumetric heat capacity [J.m-3.K-1]")
T_amb_rom = pybamm.Parameter("Ambient temperature [K]")

# =====================================================================
# Thermal Network Parameters & Strict Energy Conservation
# =====================================================================
k_int_rom = 1500.0  # Effective internal thermal conductance (mimicking radial lag)
k_ext_rom = 2850.0  # External convection coefficient (e.g. 5 °C chamber)

v_core = 0.25       # Volumetric fraction of the core node (25%)
v_surf = 0.75       # Volumetric fraction of the surface/case node (75%)

# Simulating 'current crowding' at the core/tabs during the 200A short circuit
hotspot_factor_rom = 2.8

# [CRITICAL]: Enforce 100% total energy conservation across the cell.
# Constraint: v_core * hotspot_factor + v_surf * surf_factor = 1.0
surf_factor_rom = (1.0 - v_core * hotspot_factor_rom) / v_surf

# Formulate the Reduced Order Model PDEs
# 1. Core Node: Localised heat generation minus radial conduction outwards
dT_core_dt_rom = (hotspot_factor_rom * Q_gen_rom - (k_int_rom / v_core) * (T_core_rom - T_surf_rom)) / rho_cp_rom

# 2. Surface Node: Residual heat generation plus inward conduction minus external convection
dT_surf_dt_rom = (surf_factor_rom * Q_gen_rom + (k_int_rom / v_surf) * (T_core_rom - T_surf_rom) - (k_ext_rom / v_surf) * (T_surf_rom - T_amb_rom)) / rho_cp_rom

# Inject the bespoke ODE system into the PyBaMM computational graph
model_rom.rhs[T_core_rom] = dT_core_dt_rom
model_rom.rhs[T_surf_rom] = dT_surf_dt_rom

# Apply initial conditions
model_rom.initial_conditions[T_surf_rom] = pybamm.Scalar(initial_temperature_K)
model_rom.variables["Custom Core temp [C] ROM"] = T_core_rom - 273.15
model_rom.variables["Custom Surface temp [C] ROM"] = T_surf_rom - 273.15

# Accelerate computation using the CasADi JIT solver
solver_rom = pybamm.CasadiSolver(mode="fast with events", dt_max=0.1, rtol=1e-5, atol=1e-6)
sim_rom = pybamm.Simulation(
    model=model_rom,
    experiment=experiment,
    parameter_values=param_rom,
    solver=solver_rom,
    var_pts=var_pts
)

solution_rom = sim_rom.solve(initial_soc=initial_soc)

# Extract ROM Data
t_rom = solution_rom["Time [s]"].entries
temp_core_rom_C = solution_rom["Custom Core temp [C] ROM"].entries
temp_surf_rom_C = solution_rom["Custom Surface temp [C] ROM"].entries
print(f"[INFO] ROM computation solved in {solution_rom.solve_time.value:.3f} seconds!\n")


# =====================================================================
# 3. Plotting: Benchmarking Validation
# =====================================================================
print("[INFO] Generating benchmarking visualisation...")
plt.figure(figsize=(9, 6))

plt.plot(t_rom, temp_core_rom_C, 'r-', linewidth=3, label="ROM: Core (Internal Hotspot)")
plt.plot(t_rom, temp_surf_rom_C, 'b--', linewidth=2.5, label="ROM: Surface (Observed)")
plt.plot(t_base, temp_base_C, 'k:', linewidth=2.5, label="Baseline (Lumped Average)")

# Separator safety threshold
plt.axhline(y=140, color='gray', linestyle='-.', alpha=0.8, linewidth=2, label="Separator Melting Point (140°C)")

plt.title("Benchmarking: Lumped Baseline vs. Two-Way Coupled Radial ROM", fontsize=14, fontweight='bold')
plt.xlabel("Time from Short Circuit [s]", fontsize=12)
plt.ylabel("Cell Temperature [°C]", fontsize=12)
plt.legend(loc='upper left', fontsize=11, facecolor='white', framealpha=0.9)
plt.grid(True, linestyle='--', alpha=0.6)
plt.tight_layout()
plt.show()