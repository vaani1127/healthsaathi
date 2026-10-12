# Results of the registered v1 experiments

This document reports the final experiments exactly as registered in
`experiments/PREREGISTRATION.md` at commit 14f5feb (tag `prereg-v1`, timestamped with
OpenTimestamps). Every number below comes from files committed in `experiments/results/v1/`
(written by `make exp` and the run scripts in `experiments/results/v1/execution/`) or from
`experiments/audit/v1/`. Tables in this file were generated from those files; figures are drawn
from them by `e2d_experiments.paper_figures`. All data are synthetic (SaathiBench v1); no
real patient data were used.

Intervals are 95% bootstrap intervals over the 10 seeds (10,000 resamples) unless stated
otherwise. "Clinic cluster interval" is the registered second interval, a bootstrap over test
clinics pooled across seeds.

## 1. What was run

| Run | Data | Methods | Seeds |
|---|---|---|---|
| Main | SaathiBench v1, 30 clinics, 180 days | B0 to B5, E2D, upper bound | 10 |
| `mimicry_0`, `mimicry_05`, `mimicry_1` | v1 simulated again with every campaign at one mimicry level | all | 10 each |
| `hard_neg_half`, `hard_neg_double` | v1 simulated again with hard-negative rates at half and double | all | 10 each |
| `templates_core` | main data, core templates only | E2D, B5, upper bound (B0 to B4 unchanged) | 10 |
| 10 feature and training ablations | main data | E2D only (baselines unchanged) | 10 each |
| Scorer variants | main data | E2D with ECOD, E2D with COPOD | 10 |

The changes made after registration are listed in section 9 of the pre-registration with their
reasons and effects. In short: resumable runs, a memory-only fix to B3 (bit-for-bit identical
sequences, sampled rows and scores), the execution environment (one thread setting for every
run), running unchanged-data ablations for E2D only, and running `mimicry_0` without the
separability-audit gate because its data fail the audit (it is reported as unaudited). The
frozen paths (`sim/`, `packages/e2d-core`, `experiments/configs`) are unchanged since 14f5feb.

Checks that the numbers are what the pipeline produced:

- The simulated data on the execution machine matched the registered audit exactly (seed 0
  audit, every field).
- Simulating with 11 workers and with 1 worker gave byte-identical Parquet files (1,182 files).
- E2D's recall@5 was recomputed independently on every seed and equals the registered value
  exactly (`diagnostics2.json`).
- Three re-simulated hard-negative runs on a second machine had the same configuration
  fingerprint, row counts and measured shares.

## 2. Hypotheses (section 5 of the pre-registration)

Tests are one-sided paired Wilcoxon signed-rank tests over the 10 seeds; effect size is the
matched-pairs rank-biserial r. With 10 seeds the smallest possible one-sided p is 1/1024 =
0.00098. No zero differences were dropped except where stated.

| Hypothesis | Registered criterion | Result | Verdict |
|---|---|---|---|
| H1 coverage | mean coverage of benign accesses >= 0.90 (temporal) | 0.970 [0.970, 0.970] | met |
| H1 false explanation | mean false-explanation rate of non-forging attacks < 0.10 (temporal) | 0.172 [0.160, 0.185] | **not met** |
| **H1 overall** | both parts | coverage met, false explanation not met | **not supported** |
| **H2 primary** | E2D recall@5 > best raw baseline, temporal split, B = 5, uncorrected | E2D 0.216 vs B0 rules 0.122; p = 0.00098; r = 1.00 | **supported** |
| H2 secondary | the other 11 split and budget pairs, Holm-corrected together | E2D higher in all 11; Holm p = 0.0107 in every cell; r = 0.96 to 1.00 | supported in all 11 |
| H3 hard attacks | gain over best baseline larger for types 1, 2, 6 than for 3, 4, 5, 7, 9 (m >= 1/2) | mean difference of gains 0.178; p = 0.00098; 0 seeds dropped | **supported** |
| H4 forgery features | type 10 recall@5 with forgery features > without, temporal and attack splits, Holm over 2 | temporal 0.372 vs 0.230; attack 0.357 vs 0.225 (1 zero dropped); Holm p = 0.0020 both | **supported** |
| H5 residual detector | E2D recall@5 > B5 (explanation only), temporal, B = 5 | 0.216 vs 0.140; p = 0.00098; r = 1.00 | **supported** |

The best raw-feature baseline (the one H2 compares against) is B0 rules in every split and
budget. Figure `fig_hypotheses` shows every paired difference with its interval.

### H2 for every split and budget

| Split | B | E2D | Best raw baseline | p | Holm p | r |
|---|---|---|---|---|---|---|
| temporal | 3 | 0.133 | B0 rules 0.066 | 0.00098 | 0.0107 | 1.00 |
| temporal (primary) | 5 | 0.216 | B0 rules 0.122 | 0.00098 | 0.0010 | 1.00 |
| temporal | 10 | 0.378 | B0 rules 0.230 | 0.00098 | 0.0107 | 1.00 |
| clinic | 3 | 0.110 | B0 rules 0.072 | 0.00098 | 0.0107 | 1.00 |
| clinic | 5 | 0.182 | B0 rules 0.129 | 0.00195 | 0.0107 | 0.96 |
| clinic | 10 | 0.322 | B0 rules 0.239 | 0.00098 | 0.0107 | 1.00 |
| attack | 3 | 0.135 | B0 rules 0.066 | 0.00098 | 0.0107 | 1.00 |
| attack | 5 | 0.217 | B0 rules 0.122 | 0.00098 | 0.0107 | 1.00 |
| attack | 10 | 0.380 | B0 rules 0.230 | 0.00098 | 0.0107 | 1.00 |
| user | 3 | 0.147 | B0 rules 0.113 | 0.00098 | 0.0107 | 1.00 |
| user | 5 | 0.227 | B0 rules 0.186 | 0.00098 | 0.0107 | 1.00 |
| user | 10 | 0.377 | B0 rules 0.312 | 0.00098 | 0.0107 | 1.00 |

## 3. Detection at every split and budget

Recall@B is the share of attack accesses that are among the B alerts of their clinic-day;
precision@B is the share of alerts that are attacks (section 4 of the pre-registration). Break-glass
accesses and attack type 8 are excluded from these metrics for every method.

E2D has the highest recall of all unsupervised methods in 11 of the 12 split and budget cells. The
exception is the user split at B = 10, where B5 (explanation only, not a raw-feature baseline) is
level with it (0.386 vs 0.377). The supervised upper bound, which sees labelled training attacks,
reaches roughly twice E2D's recall; it is a ceiling, not a competitor. Figures:
`fig_recall_by_budget`, `fig_main_result`.

#### Recall@3

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.133 [0.123, 0.144] | 0.110 [0.099, 0.121] | 0.135 [0.124, 0.149] | 0.147 [0.133, 0.162] |
| B0 rules | 0.066 [0.061, 0.072] | 0.072 [0.066, 0.078] | 0.066 [0.061, 0.072] | 0.113 [0.102, 0.124] |
| B1 IForest | 0.054 [0.046, 0.062] | 0.047 [0.041, 0.055] | 0.053 [0.046, 0.061] | 0.097 [0.085, 0.107] |
| B2 IForest per role | 0.060 [0.052, 0.068] | 0.066 [0.058, 0.075] | 0.060 [0.053, 0.068] | 0.103 [0.090, 0.116] |
| B3 sequence VAE | 0.035 [0.032, 0.038] | 0.037 [0.032, 0.042] | 0.035 [0.032, 0.039] | 0.061 [0.056, 0.066] |
| B4 co-access CF | 0.006 [0.005, 0.007] | 0.026 [0.022, 0.030] | 0.006 [0.005, 0.007] | 0.027 [0.024, 0.030] |
| B5 explanation only | 0.089 [0.082, 0.098] | 0.084 [0.077, 0.092] | 0.089 [0.082, 0.098] | 0.133 [0.122, 0.144] |
| Upper bound (LightGBM) | 0.313 [0.298, 0.326] | 0.294 [0.282, 0.306] | 0.307 [0.292, 0.321] | 0.325 [0.308, 0.342] |

#### Recall@5

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.216 [0.202, 0.232] | 0.182 [0.166, 0.197] | 0.217 [0.201, 0.236] | 0.227 [0.208, 0.247] |
| B0 rules | 0.122 [0.112, 0.132] | 0.129 [0.119, 0.139] | 0.122 [0.112, 0.132] | 0.186 [0.170, 0.202] |
| B1 IForest | 0.095 [0.083, 0.107] | 0.085 [0.074, 0.097] | 0.096 [0.083, 0.109] | 0.158 [0.142, 0.173] |
| B2 IForest per role | 0.102 [0.090, 0.114] | 0.113 [0.101, 0.126] | 0.102 [0.091, 0.114] | 0.169 [0.151, 0.187] |
| B3 sequence VAE | 0.060 [0.055, 0.066] | 0.061 [0.054, 0.068] | 0.060 [0.055, 0.066] | 0.103 [0.096, 0.110] |
| B4 co-access CF | 0.008 [0.006, 0.009] | 0.041 [0.035, 0.048] | 0.008 [0.006, 0.009] | 0.041 [0.036, 0.045] |
| B5 explanation only | 0.140 [0.129, 0.152] | 0.131 [0.121, 0.140] | 0.140 [0.129, 0.152] | 0.213 [0.199, 0.227] |
| Upper bound (LightGBM) | 0.449 [0.430, 0.467] | 0.420 [0.404, 0.438] | 0.441 [0.422, 0.458] | 0.464 [0.441, 0.488] |

#### Recall@10

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.378 [0.357, 0.399] | 0.322 [0.301, 0.340] | 0.380 [0.358, 0.404] | 0.377 [0.346, 0.408] |
| B0 rules | 0.230 [0.213, 0.245] | 0.239 [0.225, 0.254] | 0.230 [0.213, 0.245] | 0.312 [0.290, 0.333] |
| B1 IForest | 0.180 [0.159, 0.202] | 0.169 [0.150, 0.191] | 0.181 [0.160, 0.204] | 0.267 [0.244, 0.289] |
| B2 IForest per role | 0.187 [0.169, 0.205] | 0.208 [0.189, 0.226] | 0.185 [0.168, 0.202] | 0.284 [0.259, 0.309] |
| B3 sequence VAE | 0.126 [0.118, 0.136] | 0.128 [0.117, 0.138] | 0.125 [0.117, 0.136] | 0.203 [0.190, 0.216] |
| B4 co-access CF | 0.009 [0.008, 0.011] | 0.063 [0.055, 0.072] | 0.009 [0.008, 0.011] | 0.063 [0.056, 0.070] |
| B5 explanation only | 0.271 [0.256, 0.286] | 0.250 [0.238, 0.261] | 0.271 [0.256, 0.286] | 0.386 [0.360, 0.409] |
| Upper bound (LightGBM) | 0.642 [0.619, 0.662] | 0.608 [0.588, 0.628] | 0.630 [0.608, 0.650] | 0.659 [0.629, 0.690] |

#### Precision@3

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.099 [0.091, 0.109] | 0.063 [0.058, 0.067] | 0.101 [0.091, 0.113] | 0.035 [0.031, 0.039] |
| B0 rules | 0.051 [0.046, 0.055] | 0.042 [0.038, 0.046] | 0.051 [0.046, 0.055] | 0.032 [0.029, 0.036] |
| B1 IForest | 0.040 [0.035, 0.046] | 0.027 [0.024, 0.032] | 0.040 [0.035, 0.046] | 0.022 [0.019, 0.025] |
| B2 IForest per role | 0.045 [0.039, 0.051] | 0.038 [0.033, 0.044] | 0.045 [0.040, 0.050] | 0.023 [0.020, 0.027] |
| B3 sequence VAE | 0.026 [0.024, 0.028] | 0.021 [0.018, 0.024] | 0.026 [0.024, 0.029] | 0.014 [0.012, 0.015] |
| B4 co-access CF | 0.005 [0.004, 0.006] | 0.015 [0.013, 0.018] | 0.005 [0.004, 0.006] | 0.006 [0.005, 0.007] |
| B5 explanation only | 0.067 [0.060, 0.074] | 0.050 [0.045, 0.054] | 0.067 [0.060, 0.074] | 0.036 [0.032, 0.040] |
| Upper bound (LightGBM) | 0.234 [0.216, 0.249] | 0.169 [0.160, 0.178] | 0.230 [0.212, 0.245] | 0.073 [0.067, 0.079] |

#### Precision@5

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.097 [0.089, 0.105] | 0.063 [0.058, 0.066] | 0.097 [0.089, 0.107] | 0.033 [0.029, 0.037] |
| B0 rules | 0.058 [0.053, 0.063] | 0.046 [0.043, 0.051] | 0.058 [0.053, 0.063] | 0.035 [0.031, 0.039] |
| B1 IForest | 0.042 [0.037, 0.048] | 0.029 [0.026, 0.033] | 0.043 [0.037, 0.049] | 0.022 [0.019, 0.025] |
| B2 IForest per role | 0.046 [0.040, 0.051] | 0.039 [0.035, 0.044] | 0.046 [0.041, 0.051] | 0.023 [0.020, 0.026] |
| B3 sequence VAE | 0.027 [0.025, 0.029] | 0.021 [0.019, 0.024] | 0.027 [0.025, 0.029] | 0.014 [0.012, 0.016] |
| B4 co-access CF | 0.003 [0.003, 0.004] | 0.014 [0.012, 0.017] | 0.003 [0.003, 0.004] | 0.006 [0.005, 0.006] |
| B5 explanation only | 0.063 [0.057, 0.069] | 0.047 [0.043, 0.050] | 0.063 [0.057, 0.069] | 0.036 [0.032, 0.039] |
| Upper bound (LightGBM) | 0.202 [0.187, 0.214] | 0.146 [0.137, 0.153] | 0.198 [0.183, 0.210] | 0.063 [0.058, 0.068] |

#### Precision@10

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.084 [0.079, 0.090] | 0.056 [0.052, 0.058] | 0.085 [0.079, 0.091] | 0.028 [0.025, 0.031] |
| B0 rules | 0.060 [0.056, 0.064] | 0.048 [0.044, 0.051] | 0.060 [0.056, 0.064] | 0.036 [0.032, 0.039] |
| B1 IForest | 0.040 [0.035, 0.045] | 0.029 [0.026, 0.033] | 0.040 [0.036, 0.046] | 0.018 [0.016, 0.021] |
| B2 IForest per role | 0.042 [0.037, 0.046] | 0.036 [0.033, 0.040] | 0.041 [0.037, 0.045] | 0.019 [0.017, 0.022] |
| B3 sequence VAE | 0.028 [0.027, 0.030] | 0.022 [0.020, 0.024] | 0.028 [0.026, 0.030] | 0.014 [0.012, 0.015] |
| B4 co-access CF | 0.002 [0.002, 0.003] | 0.011 [0.010, 0.013] | 0.002 [0.002, 0.003] | 0.004 [0.004, 0.005] |
| B5 explanation only | 0.062 [0.057, 0.066] | 0.046 [0.043, 0.048] | 0.062 [0.057, 0.066] | 0.037 [0.033, 0.040] |
| Upper bound (LightGBM) | 0.144 [0.134, 0.152] | 0.106 [0.100, 0.111] | 0.141 [0.132, 0.149] | 0.045 [0.041, 0.048] |

#### Recall@5 with the clinic cluster interval

| Method | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| **E2D** | 0.216 [0.185, 0.247] | 0.182 [0.152, 0.211] | 0.217 [0.185, 0.248] | 0.227 [0.197, 0.255] |
| B0 rules | 0.122 [0.097, 0.148] | 0.129 [0.100, 0.159] | 0.122 [0.097, 0.147] | 0.186 [0.152, 0.221] |
| B1 IForest | 0.095 [0.075, 0.114] | 0.085 [0.066, 0.104] | 0.096 [0.077, 0.115] | 0.158 [0.126, 0.192] |
| B2 IForest per role | 0.102 [0.084, 0.122] | 0.113 [0.093, 0.135] | 0.102 [0.084, 0.121] | 0.169 [0.141, 0.199] |
| B3 sequence VAE | 0.060 [0.051, 0.069] | 0.061 [0.050, 0.073] | 0.060 [0.050, 0.069] | 0.103 [0.085, 0.121] |
| B4 co-access CF | 0.008 [0.006, 0.009] | 0.041 [0.032, 0.052] | 0.008 [0.006, 0.009] | 0.041 [0.032, 0.051] |
| B5 explanation only | 0.140 [0.128, 0.153] | 0.131 [0.115, 0.147] | 0.140 [0.128, 0.153] | 0.213 [0.184, 0.239] |
| Upper bound (LightGBM) | 0.449 [0.429, 0.473] | 0.420 [0.398, 0.444] | 0.441 [0.420, 0.464] | 0.464 [0.438, 0.485] |

#### Other metrics, temporal split, B = 5

| Method | PR-AUC | Campaigns detected | Median hours to first alert | Alerts per 1,000 accesses |
|---|---|---|---|---|
| **E2D** | 0.148 [0.130, 0.164] | 0.732 [0.707, 0.759] | 24.1 [24.1, 24.2] | 4.02 [4.01, 4.03] |
| B0 rules | 0.052 [0.042, 0.063] | 0.510 [0.491, 0.530] | 0.2 [0.1, 0.3] | 3.79 [3.78, 3.81] |
| B1 IForest | 0.032 [0.025, 0.038] | 0.375 [0.348, 0.401] | 0.4 [0.3, 0.5] | 4.02 [4.01, 4.03] |
| B2 IForest per role | 0.086 [0.073, 0.100] | 0.459 [0.439, 0.481] | 0.5 [0.4, 0.6] | 4.02 [4.01, 4.03] |
| B3 sequence VAE | 0.101 [0.080, 0.121] | 0.420 [0.400, 0.440] | 1.5 [0.1, 4.2] | 4.02 [4.01, 4.03] |
| B4 co-access CF | 0.002 [0.002, 0.002] | 0.080 [0.070, 0.090] | 143.4 [81.9, 216.8] | 4.02 [4.01, 4.03] |
| B5 explanation only | 0.173 [0.148, 0.197] | 0.530 [0.501, 0.558] | 0.5 [0.4, 0.6] | 4.01 [4.01, 4.02] |
| Upper bound (LightGBM) | 0.883 [0.869, 0.897] | 0.957 [0.947, 0.967] | 24.0 [24.0, 24.0] | 4.02 [4.01, 4.03] |


## 4. Attack types, the upper bound and the review queue

Type 8 (break-glass abuse) is excluded from the budgeted metrics and reported as review-queue outcomes: every type 8 access went to the 24-hour review queue in every split. Figure: `fig_recall_by_type`.
#### Recall@5 by attack type, temporal split (mean of 10 seeds)

| Type | **E2D** | B0 rules | B1 IForest | B2 IForest per role | B3 sequence VAE | B4 co-access CF | B5 explanation only | Upper bound (LightGBM) |
|---|---|---|---|---|---|---|---|---|
| 1 Relative | 0.585 | 0.140 | 0.091 | 0.108 | 0.037 | 0.024 | 0.222 | 0.735 |
| 2 Colleague | 0.346 | 0.144 | 0.114 | 0.139 | 0.035 | 0.023 | 0.244 | 0.734 |
| 3 Overreach | 0.123 | 0.092 | 0.042 | 0.067 | 0.133 | 0.008 | 0.087 | 0.294 |
| 4 Post-care | 0.181 | 0.064 | 0.020 | 0.053 | 0.023 | 0.002 | 0.153 | 0.505 |
| 5 Bulk export | 0.150 | 0.097 | 0.111 | 0.062 | 0.040 | 0.005 | 0.069 | 0.164 |
| 6 Low and slow | 0.278 | 0.163 | 0.136 | 0.153 | 0.048 | 0.009 | 0.215 | 0.654 |
| 7 Shared login | 0.100 | 0.085 | 0.042 | 0.049 | 0.111 | 0.006 | 0.068 | 0.249 |
| 9 Edit after sign | 0.238 | 0.161 | 0.076 | 0.259 | 0.070 | 0.004 | 0.127 | 0.755 |
| 10 Forgery | 0.372 | 0.191 | 0.129 | 0.151 | 0.065 | 0.001 | 0.024 | 0.599 |

#### Upper bound (LightGBM) recall@5 by attack type, every split

| Type | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| 1 Relative | 0.735 [0.680, 0.787] | 0.735 [0.701, 0.769] | 0.745 [0.695, 0.790] | 0.801 [0.751, 0.845] |
| 2 Colleague | 0.734 [0.699, 0.766] | 0.761 [0.711, 0.814] | 0.742 [0.705, 0.774] | 0.783 [0.714, 0.854] |
| 3 Overreach | 0.294 [0.253, 0.335] | 0.327 [0.289, 0.366] | 0.292 [0.253, 0.332] | 0.310 [0.274, 0.350] |
| 4 Post-care | 0.505 [0.488, 0.524] | 0.468 [0.424, 0.516] | 0.505 [0.487, 0.524] | 0.512 [0.473, 0.555] |
| 5 Bulk export | 0.164 [0.150, 0.178] | 0.173 [0.152, 0.195] | 0.168 [0.154, 0.183] | 0.197 [0.165, 0.231] |
| 6 Low and slow | 0.654 [0.636, 0.673] | 0.638 [0.614, 0.667] | 0.641 [0.623, 0.661] | 0.695 [0.679, 0.711] |
| 7 Shared login | 0.249 [0.216, 0.290] | 0.243 [0.207, 0.283] | 0.249 [0.216, 0.289] | 0.263 [0.234, 0.295] |
| 9 Edit after sign | 0.755 [0.716, 0.796] | 0.804 [0.765, 0.840] | 0.754 [0.709, 0.795] | 0.868 [0.833, 0.903] |
| 10 Forgery | 0.599 [0.545, 0.650] | 0.666 [0.592, 0.728] | 0.353 [0.289, 0.435] | 0.760 [0.703, 0.821] |

#### Review queue (break-glass), per split

| Split | Break-glass accesses in the test part | Share that are attacks | Type 8 accesses that went to the review queue |
|---|---|---|---|
| temporal | 726.6 [693.3, 763.7] | 0.257 [0.217, 0.293] | 1.000 [1.000, 1.000] |
| clinic | 700.8 [668.7, 734.2] | 0.224 [0.191, 0.260] | 1.000 [1.000, 1.000] |
| attack | 726.6 [693.3, 763.7] | 0.257 [0.217, 0.293] | 1.000 [1.000, 1.000] |
| user | 667.4 [607.6, 734.9] | 0.203 [0.172, 0.233] | 1.000 [1.000, 1.000] |

#### Explanation coverage and false-explanation rate, per split

| Split | Coverage of benign accesses | False-explanation rate (types 1-7, 9) |
|---|---|---|
| temporal | 0.970 [0.970, 0.970] | 0.172 [0.160, 0.185] |
| clinic | 0.968 [0.967, 0.968] | 0.190 [0.177, 0.208] |
| attack | 0.970 [0.970, 0.970] | 0.172 [0.160, 0.185] |
| user | 0.967 [0.966, 0.968] | 0.179 [0.167, 0.190] |


## 5. Ablations (section 7, descriptive)

Ablations that change only E2D were run for E2D only; the baselines there are those of the main run (section 9 of the pre-registration). `mimicry_0` is unaudited. Figures: `fig_ablations`, `fig_resimulated`, `fig_h4_forgery_features`, `fig_forgery_flags`.

#### E2D recall@5 in each ablation that uses the main data

| Ablation | Temporal | Clinic | Attack | User |
|---|---|---|---|---|
| E2D as registered | 0.216 [0.202, 0.232] | 0.182 [0.166, 0.197] | 0.217 [0.201, 0.236] | 0.227 [0.208, 0.247] |
| No forgery features | 0.193 [0.183, 0.203] | 0.175 [0.164, 0.185] | 0.194 [0.182, 0.205] | 0.240 [0.221, 0.260] |
| No graph features | 0.179 [0.165, 0.194] | 0.147 [0.137, 0.159] | 0.183 [0.168, 0.200] | 0.216 [0.196, 0.235] |
| Core templates only | 0.141 [0.131, 0.150] | 0.095 [0.086, 0.104] | 0.145 [0.134, 0.156] | 0.176 [0.158, 0.192] |
| Role one-hot | 0.189 [0.174, 0.203] | 0.164 [0.150, 0.177] | 0.190 [0.174, 0.205] | 0.234 [0.217, 0.250] |
| One model per role | 0.279 [0.262, 0.294] | 0.251 [0.231, 0.271] | 0.270 [0.256, 0.286] | 0.322 [0.299, 0.348] |
| Per role, 10% of training | 0.051 [0.044, 0.059] | 0.040 [0.035, 0.045] | 0.052 [0.044, 0.060] | 0.139 [0.122, 0.157] |
| Per role, 25% | 0.134 [0.071, 0.201] | 0.072 [0.037, 0.118] | 0.071 [0.049, 0.109] | 0.180 [0.138, 0.233] |
| Per role, 50% | 0.269 [0.251, 0.288] | 0.258 [0.234, 0.280] | 0.272 [0.252, 0.294] | 0.325 [0.304, 0.348] |
| Behaviour window 1 h | 0.191 [0.178, 0.206] | 0.174 [0.163, 0.184] | 0.197 [0.184, 0.212] | 0.206 [0.188, 0.226] |
| Behaviour window 24 h | 0.192 [0.178, 0.207] | 0.160 [0.150, 0.170] | 0.195 [0.182, 0.212] | 0.212 [0.192, 0.234] |
| Behaviour window 7 d | 0.195 [0.181, 0.210] | 0.167 [0.156, 0.179] | 0.199 [0.185, 0.214] | 0.221 [0.202, 0.242] |
| E2D, ECOD scorer | 0.170 [0.159, 0.181] | 0.154 [0.145, 0.165] | 0.171 [0.160, 0.182] | 0.253 [0.236, 0.273] |
| E2D, COPOD scorer | 0.185 [0.173, 0.198] | 0.177 [0.167, 0.187] | 0.186 [0.175, 0.199] | 0.262 [0.243, 0.283] |

`templates_core` also re-ran B5 and the upper bound (temporal, recall@5): B5 0.146 [0.136, 0.156], upper bound 0.451 [0.432, 0.468]; coverage 0.946 [0.946, 0.946], false-explanation rate 0.027 [0.023, 0.031].

#### Re-simulated ablations: recall@5, temporal split, every method

| Method | Main (v1) | Mimicry 0 (unaudited) | Mimicry 0.5 | Mimicry 1 | Hard neg. half | Hard neg. double |
|---|---|---|---|---|---|---|
| **E2D** | 0.216 [0.202, 0.232] | 0.211 [0.195, 0.229] | 0.185 [0.170, 0.199] | 0.249 [0.227, 0.272] | 0.262 [0.246, 0.280] | 0.172 [0.159, 0.185] |
| B0 rules | 0.122 [0.112, 0.132] | 0.211 [0.199, 0.224] | 0.062 [0.056, 0.067] | 0.048 [0.040, 0.056] | 0.122 [0.115, 0.127] | 0.115 [0.108, 0.122] |
| B1 IForest | 0.095 [0.083, 0.107] | 0.142 [0.135, 0.149] | 0.049 [0.043, 0.057] | 0.050 [0.040, 0.061] | 0.092 [0.085, 0.099] | 0.088 [0.078, 0.098] |
| B2 IForest per role | 0.102 [0.090, 0.114] | 0.177 [0.164, 0.191] | 0.058 [0.053, 0.062] | 0.034 [0.029, 0.038] | 0.103 [0.094, 0.114] | 0.112 [0.104, 0.119] |
| B3 sequence VAE | 0.060 [0.055, 0.066] | 0.070 [0.064, 0.075] | 0.050 [0.042, 0.057] | 0.033 [0.027, 0.039] | 0.055 [0.051, 0.060] | 0.051 [0.045, 0.058] |
| B4 co-access CF | 0.008 [0.006, 0.009] | 0.023 [0.020, 0.026] | 0.005 [0.004, 0.006] | 0.001 [0.001, 0.002] | 0.009 [0.008, 0.011] | 0.010 [0.009, 0.011] |
| B5 explanation only | 0.140 [0.129, 0.152] | 0.149 [0.140, 0.159] | 0.106 [0.100, 0.113] | 0.143 [0.131, 0.155] | 0.150 [0.139, 0.162] | 0.119 [0.111, 0.129] |
| Upper bound (LightGBM) | 0.449 [0.430, 0.467] | 0.349 [0.328, 0.371] | 0.441 [0.431, 0.451] | 0.757 [0.742, 0.773] | 0.441 [0.423, 0.458] | 0.449 [0.429, 0.471] |
| Coverage | 0.970 [0.970, 0.970] | 0.970 [0.970, 0.970] | 0.970 [0.970, 0.970] | 0.970 [0.970, 0.970] | 0.978 [0.978, 0.978] | 0.954 [0.954, 0.955] |
| False-explanation rate | 0.172 [0.160, 0.185] | 0.167 [0.153, 0.182] | 0.165 [0.151, 0.178] | 0.157 [0.144, 0.173] | 0.172 [0.157, 0.186] | 0.164 [0.147, 0.181] |

#### H2-style test inside each re-simulated ablation (temporal, B = 5; descriptive)

| Ablation | E2D | Best raw baseline | p (one-sided Wilcoxon) | r |
|---|---|---|---|---|
| mimicry_0 | 0.211 | B0 rules 0.211 | 0.5391 | -0.02 |
| mimicry_05 | 0.185 | B0 rules 0.062 | 0.0010 | 1.00 |
| mimicry_1 | 0.249 | B1 IForest 0.050 | 0.0010 | 1.00 |
| hard_neg_half | 0.262 | B0 rules 0.122 | 0.0010 | 1.00 |
| hard_neg_double | 0.172 | B0 rules 0.115 | 0.0010 | 1.00 |

Measured hard-negative share (mean over 10 seeds): hard_neg_half 0.0158, main 0.0273, hard_neg_double 0.0489

## 6. Diagnostics (not registered results)

These come from `experiments/results/v1/diagnostics.json` and `diagnostics2.json` (temporal
split, 10 seeds). They explain the registered results; they do not change them.

**Why H1's false-explanation part fails.** Per attack type, the share of attack accesses that
the templates explain (sigma >= 0.5) is: type 9 (edit after sign) 0.653, type 5 (bulk export)
0.258, type 6 (low and slow) 0.159, type 7 (shared login) 0.146, type 3 (overreach) 0.114,
type 1 (relative) 0.109, type 2 (colleague) 0.086, type 4 (post-care) 0.005. Across all falsely
explained accesses, the explaining template is the care-team template in 66% of cases, billing
in 16% and front desk in 7%. In other words, attackers who are on the patient's care team (or act
on a patient they really treat) are explained by that legitimate relationship. This is a property
of explanation-based auditing itself, not of E2D's detector: an explanation shows that a reason
may exist, not that the access was appropriate. Figures: `fig_false_explanation`,
`fig_false_explanation_templates`, `fig_sigma_distribution`.

**What E2D's false alarms are.** At B = 5 on the temporal split, 84% of E2D's benign alerts are
ordinary accesses and 16% come from the benign hard-negative scenarios (nurse lab follow-up 4.3%,
staff viewing their own record 4.1%, month-end billing 3.5%, covering doctor 2.4%, pharmacy-like
checks 1.7%). By role: lab technician 32%, doctor 31%, reception 28%, nurse 7%, clinic admin 2%.
About 61% of benign alerts are explained accesses that a forgery flag sent through the gate.
Explanation coverage of benign accesses by role: doctor 0.992, lab technician 0.999, reception
0.946, nurse 0.933, clinic admin 0.000 (no template covers clinic administrators). Figure:
`fig_benign_alerts`.

**PR-AUC.** E2D's registered PR-AUC is pooled over all clinics (temporal 0.148, user 0.018).
Averaged per clinic it is 0.179 (temporal) and 0.156 (user). E2D scores each clinic with its own
model, so scores are not comparable across clinics; the alert budget is per clinic, which is why
recall is not affected. Figure: `fig_pr_auc`.

## 7. What counts against E2D (section 8 of the pre-registration)

| Registered criterion | Outcome |
|---|---|
| Primary H2 test not supported | Not triggered: supported (p = 0.00098, r = 1.00). |
| H5 not supported | Not triggered: supported. |
| Upper bound showing the features carry little signal for some types | **Triggered for types 3, 5 and 7**: the supervised upper bound reaches only 0.29, 0.16 and 0.25 recall@5 (temporal), so the features carry little signal for overreach, bulk export and shared logins. For bulk export E2D (0.150) is close to the ceiling (0.164). |
| Coverage below 90% | Not triggered: 0.970. |
| E2D's advantage disappearing in `hard_neg_double` | Not triggered: the advantage shrinks (0.094 in the main data, 0.057 with double hard negatives) but stays significant (E2D 0.172 vs B0 0.115, p = 0.00098). |

Other weaknesses to report plainly:

- H1 is not supported (false-explanation rate 0.172 > 0.10).
- With careless attackers (`mimicry_0`, unaudited) E2D does not beat the simple rules (0.211 vs
  0.211, p = 0.54): when attacks are blatant, raw behaviour is enough.
- E2D's alerts are timed 24 hours after the access, because the forgery flags are computed then;
  baselines alert at the access (median hours to first alert: about 24 for E2D and the upper bound,
  under 2 for B0, B1, B2, B3 and B5, and 143 for B4).
- Absolute recall is modest: about one attack access in five is caught at five alerts per clinic
  per day, against about one in two for the supervised ceiling.
- In the ablations, one model per role (0.279) and per-role models on half the training data
  (0.269) score higher than E2D as registered (0.216). These are exploratory; the registered method
  is per clinic, and the paper may say only that role conditioning is a promising direction.
- The learning curve is not monotone and `learning_curve_25` is unstable across seeds
  (0.134 [0.071, 0.201]).

## 8. Limitations

- All data are synthetic. The simulator was designed by the authors; its realism parameters are
  reported in the pre-registration and the results hold only for that generator.
- One benchmark, 30 simulated clinics of three profiles, 180 days. No real hospital data.
- The false-explanation part of H1 mostly reflects how often attackers have a real care
  relationship, which the simulator decides.
- Care-team forgeries (type 10) are not covered by the no-progress and cancellation flags.
- `mimicry_0` is outside the audited regime.
- PR-AUC is pooled across clinics as registered; per-clinic values are a diagnostic.

## 9. Related work and novelty

A literature search (web search plus every paper citing Fabbri and LeFevre's explanation-based
auditing, 44 papers via Semantic Scholar) found the following closest work. It is not a full
systematic review; it must be repeated on Google Scholar and the ACM Digital Library before
submission.

- Explanation-based auditing: Fabbri and LeFevre, PVLDB 5(1), 2011 (about 94% of accesses
  explained at one hospital); Fabbri, LeFevre and Hanauer, JAMIA 2013 (diagnoses). These assume
  the evidence behind an explanation is genuine.
- Limits of explanations: Hedda, Malin, Yan and Fabbri, AMIA 2017 (16% to 44% of rule-flagged
  accesses explained at Vanderbilt; an explanation shows only that a reason may exist).
- Anomaly detection on access logs: Chen and Malin (CODASPY 2011; network analysis), Menon et al.
  (collaborative filtering), Boxwala et al. (JAMIA 2011), and recent supervised and VAE models.
- Audit prioritisation under limited budgets: Laszka et al. (2017), Yan et al. (ICDE 2018;
  signalling, 2019).
- Evasion of EHR access detectors: Xu et al., USENIX Security 2023. Their attacker injects extra
  "covering accesses" to fool graph-based detectors; it does not fabricate clinical workflow
  evidence and does not target explanation-based auditing.
- Blockchain audit trails for EHRs (for example Heliyon 2024) log accesses or enforce policy but do
  not detect misuse or let patients verify accesses themselves; national portals (Norway, Estonia,
  Denmark) show patients their access logs without cryptographic verification.
- No public labelled benchmark of EHR access misuse was found.

What these results support as contributions, to our knowledge:

1. Forgery-aware explanation auditing: an insider who fabricates the workflow evidence that makes
   an access look explained, and forgery indicators that restore detection (H4: type 10 recall
   0.23 to 0.37). This attack differs from Xu et al., who add accesses rather than evidence.
2. Explain-then-detect under a fixed alert budget, compared fairly and pre-registered: better than
   explanation-only auditing (H5) and than raw-behaviour detectors (H2) on every split and budget.
3. SaathiBench: an open, labelled, pre-registered benchmark for EHR access misuse with a
   separability audit and benign hard negatives.
4. Supporting: patient-verifiable access receipts anchored on public witnesses.

Claims to avoid: that explanation-based auditing, anomaly detection on access logs or blockchain
audit logs are new, and any claim about real hospitals.

## 10. Figures

All in `paper/figures` as PDF and 300 DPI PNG, drawn by `uv run python -m
e2d_experiments.paper_figures` from the committed results.

| File | Shows |
|---|---|
| `fig_recall_by_budget` | recall at B = 3, 5, 10 for every method and split |
| `fig_main_result` | recall@5 and precision@5, temporal split |
| `fig_recall_by_type` | recall@5 per attack type and method |
| `fig_hypotheses` | paired per-seed differences behind H2, H4, H5 |
| `fig_ablations` | E2D in every ablation and scorer variant |
| `fig_resimulated` | every method across mimicry levels and hard-negative shares |
| `fig_h4_forgery_features` | H4 |
| `fig_forgery_flags` | forgery flag fire rates, benign vs each type 10 variant (audit) |
| `fig_false_explanation`, `fig_false_explanation_templates` | why H1's false-explanation part fails |
| `fig_sigma_distribution` | explanation strength, benign vs attacks |
| `fig_benign_alerts` | what E2D's false alarms are |
| `fig_pr_auc` | pooled vs per-clinic PR-AUC |
| `recall_by_budget_*.pdf` | the figures written by the registered pipeline itself |

## 11. Reproducing

- Main run: `SIM_WORKERS=3 make exp NAME=v1 ARGS="seeds=[0,1,2,3,4,5,6,7,8,9] workers=4
  export.tables=paper/tables export.figures=paper/figures"` (the exact commands used are in
  `experiments/results/v1/execution/run_all.sh` and `run_mimicry0.sh`).
- Hypotheses: `uv run python -m e2d_experiments.report experiments/outputs/v1 --export
  paper/tables`.
- Figures: `uv run python -m e2d_experiments.paper_figures`.
- Every final run used 4 threads per process (`POLARS_MAX_THREADS`, `OMP_NUM_THREADS`,
  `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` = 4); thread counts can change the last digits.
