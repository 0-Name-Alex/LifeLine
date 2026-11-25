"""Offline laptop training. Produces both models and a held-out-session evaluation."""
import argparse
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import classification_report, confusion_matrix, precision_recall_fscore_support
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from features import FEATURES, WINDOW_SECONDS, create_sessions, features_from_log, proxy_labels

ROOT = Path(__file__).resolve().parent
LABELS = ['Normal', 'Warning', 'High Risk']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=ROOT / 'training_data' / 'health_monitor_log.csv')
    parser.add_argument('--models', type=Path, default=ROOT / 'models')
    parser.add_argument('--results', type=Path, default=ROOT / 'results')
    args = parser.parse_args()
    args.models.mkdir(parents=True, exist_ok=True)
    args.results.mkdir(parents=True, exist_ok=True)
    df = create_sessions(pd.read_csv(args.data))
    X = features_from_log(df)
    y = proxy_labels(df)
    sessions = sorted(df.session.unique())
    if len(sessions) < 4:
        raise SystemExit('Need >=4 separate recording sessions (>60 s apart) to evaluate without nearby-sample leakage.')
    # Chronological session split: train on earlier 60%, test on later 40%.
    # For supplied dataset with 10 sessions: train sessions 1-6, test 7-10.
    cutoff = max(2, int(len(sessions) * 0.6))
    train_sessions = sessions[:cutoff]
    test_sessions = sessions[cutoff:]
    tr = df.session.isin(train_sessions).to_numpy()
    te = df.session.isin(test_sessions).to_numpy()
    Xtr, Xte = X.loc[tr], X.loc[te]
    ytr, yte = y.loc[tr], y.loc[te]
    if ytr.nunique() < 2:
        raise SystemExit('Training sessions contain only one proxy risk class. Collect additional varied sessions.')

    classifier = Pipeline([
        ('impute', SimpleImputer(strategy='median', keep_empty_features=True)),
        ('rf', RandomForestClassifier(n_estimators=200, class_weight='balanced_subsample',
                                      min_samples_leaf=3, n_jobs=-1, random_state=42)),
    ])
    classifier.fit(Xtr, ytr)
    pred = classifier.predict(Xte)
    report = classification_report(yte, pred, labels=[0, 1, 2],
                                   target_names=LABELS, zero_division=0, output_dict=True)
    matrix = confusion_matrix(yte, pred, labels=[0, 1, 2])

    # Fit IF on *train* samples that the old rule system marked as normal.
    # The result detects deviations from this baseline, NOT confirmed danger.
    normal_X = Xtr.loc[ytr.eq(0)]
    if len(normal_X) < 50:
        raise SystemExit('Fewer than 50 normal-proxy training rows for Isolation Forest.')
    anomaly = Pipeline([
        ('impute', SimpleImputer(strategy='median', keep_empty_features=True)),
        ('scale', StandardScaler()),
        ('iforest', IsolationForest(n_estimators=200, contamination=0.05,
                                     random_state=42, n_jobs=-1)),
    ])
    anomaly.fit(normal_X)
    a_pred = anomaly.predict(Xte) == -1
    a_score = -anomaly.decision_function(Xte)  # larger = more anomalous
    # Compare anomaly flags to old rule alerts only as a *proxy agreement* metric.
    proxy_positives = yte.ne(0).to_numpy()
    precision, recall, f1, _ = precision_recall_fscore_support(
        proxy_positives, a_pred, average='binary', zero_division=0
    )

    joblib.dump(classifier, args.models / 'risk_classifier.joblib')
    joblib.dump(anomaly, args.models / 'anomaly_detector.joblib')
    metadata = {
        'feature_names': FEATURES, 'window_seconds': WINDOW_SECONDS,
        'training_sample_period_s': float(df['timestamp'].diff().dt.total_seconds().median()),
        'class_labels': dict(enumerate(LABELS)),
        'train_sessions': [int(s) for s in train_sessions],
        'test_sessions': [int(s) for s in test_sessions],
        'train_rows': int(tr.sum()), 'test_rows': int(te.sum()),
        'training_label_source': 'legacy threshold statuses, NOT independently validated hazard labels',
        'training_normal_source': 'legacy rule status normal, NOT verified normal activity',
        'temperature_warning': 'mpu_temp is chip temperature, not body/ambient temperature',
    }
    (args.models / 'metadata.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    summary = {
        'warning': 'All classification metrics measure agreement with OLD RULE LABELS, not real hazard accuracy.',
        'train_rows': int(tr.sum()), 'test_rows': int(te.sum()),
        'train_sessions': metadata['train_sessions'], 'test_sessions': metadata['test_sessions'],
        'train_label_counts': ytr.value_counts().sort_index().to_dict(),
        'test_label_counts': yte.value_counts().sort_index().to_dict(),
        'random_forest_report_vs_proxy': report,
        'random_forest_confusion_matrix_vs_proxy': matrix.tolist(),
        'isolation_forest_proxy_agreement': {
            'precision': float(precision), 'recall': float(recall), 'f1': float(f1),
            'flagged_test_samples': int(a_pred.sum()),
            'test_samples': int(len(a_pred))
        }
    }
    (args.results / 'evaluation.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    output = df.loc[te, ['timestamp', 'session', 'gas_raw', 'avg_bpm', 'finger', 'mpu_temp']].copy()
    output['proxy_label'] = yte.to_numpy()
    output['rf_prediction'] = pred
    output['iforest_anomaly'] = a_pred.astype(int)
    output['iforest_anomaly_score'] = a_score
    output.to_csv(args.results / 'heldout_predictions.csv', index=False)
    print('\n=== Training complete ===')
    print('Training sessions:', metadata['train_sessions'], 'Rows:', tr.sum())
    print('Held-out sessions:', metadata['test_sessions'], 'Rows:', te.sum())
    print('RF confusion matrix vs OLD THRESHOLD LABELS (rows actual proxy, columns prediction):\n', matrix)
    print(classification_report(yte, pred, labels=[0, 1, 2], target_names=LABELS, zero_division=0))
    print('Isolation Forest flagged:', int(a_pred.sum()), 'of', len(a_pred), 'held-out rows')
    print('Isolation Forest agreement with OLD ALERT FLAGS: precision %.3f, recall %.3f, f1 %.3f' % (precision, recall, f1))
    print('\nWARNING: These scores are NOT evidence of real hazard detection accuracy.')
    print('Saved models to:', args.models)
    print('Saved evaluation to:', args.results)

if __name__ == '__main__':
    main()
