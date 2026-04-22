"""
Budget Solver - Google Ads budget optimization tool.

Allocates monthly budget across accounts using diminishing-returns response curves
and constrained nonlinear optimization.
"""
from budget_solver.data import load_data, aggregate_weekly
from budget_solver.curves import fit_response_curve
from budget_solver.solver import optimize_budget

__version__ = "2.0.0a1"
__all__ = ["load_data", "aggregate_weekly", "fit_response_curve", "optimize_budget"]
