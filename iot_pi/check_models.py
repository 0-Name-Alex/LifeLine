"""Checks that the ML models load and predict with THIS Pi's scikit-learn.
Exit code 0 = OK. Exit code 1 = retrain with: python3 train_models.py
(install.sh does that automatically)."""
import sys
import warnings

try:
    import sklearn
    with warnings.catch_warnings():
        warnings.simplefilter('error')  # a version-mismatch warning counts as a failure
        from predictor import RiskPredictor
        p = RiskPredictor()
        r = p.predict({'accel_x': 0.1, 'accel_y': 0.2, 'accel_z': 9.8, 'gas': 1500,
                       'avg_bpm': 75, 'finger': 1, 'mpu_temp': 30.0})
    print(f"Models OK (scikit-learn {sklearn.__version__}): risk={r['risk_label']} fall={r['fall_state']}")
except Exception as exc:
    print(f"Models do not load with this scikit-learn: {type(exc).__name__}: {exc}")
    sys.exit(1)
