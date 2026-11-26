# Results (85 runs)

| Experiment | Method | n | Accuracy | Macro-F1 | Localisation |
|---|---|---|---|---|---|
| All runs | Rules | 85 | 0.9765 | 0.969 | 0.975 |
| CV variant a (5-fold) | Learned: logs_only | 55 | 0.7818 | 0.7697 | - |
| CV variant a (5-fold) | Learned: k8s_only | 55 | 0.8182 | 0.8161 | - |
| CV variant a (5-fold) | Learned: logs+k8s | 55 | 1.0 | 1.0 | - |
| Held-out variant b | Learned: logs_only | 30 | 0.3667 | 0.2167 | - |
| Held-out variant b | Learned: k8s_only | 30 | 0.8333 | 0.7143 | - |
| Held-out variant b | Learned: logs+k8s | 30 | 0.9 | 0.7959 | - |
| Held-out variant b | Rules | 30 | 1.0 | 1.0 | 1.0 |
