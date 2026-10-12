#!/bin/bash
# Runs the registered v1 experiments; safe to start again after a restart (finished seeds are kept on disk).
cd /Capstone/healthsaathi
export UV_CACHE_DIR=/dev/shm/uv-cache SIM_WORKERS=4 POLARS_MAX_THREADS=4 OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 MKL_NUM_THREADS=4
L=/Capstone/logs; mkdir -p $L /dev/shm/runs /dev/shm/scratch /dev/shm/hydra /dev/shm/locks /dev/shm/mlruns
RESIM="mimicry_0 mimicry_05 mimicry_1 hard_neg_half hard_neg_double"
SAME="no_forgery no_graph role_onehot role_per_role learning_curve_10 learning_curve_25 learning_curve_50 window_1h window_24h window_7d templates_core scorers"
[ -L experiments/outputs ] && rm experiments/outputs
for a in none $RESIM $SAME; do
  mkdir -p experiments/outputs/v1/$a
  case " $RESIM none " in *" $a "*) src=/dev/shm/runs/$a ;; *) src=/dev/shm/runs/none ;; esac
  mkdir -p $src; ln -sfn $src experiments/outputs/v1/$a/runs
done
cat > /Capstone/job.sh <<'JOB'
#!/bin/bash
# job.sh <ablation> <seed> <methods or all> <folder> <lock or -> [seeds list]
abl=$1; seed=$2; methods=$3; out=$4; lock=$5; seeds=${6:-[$seed]}
cd /Capstone/healthsaathi
extra=(); [ "$methods" != "all" ] && extra=("methods=$methods")
cmd=(.venv/bin/python -m e2d_experiments.run data=v1 ablation=$abl "seeds=$seeds" "${extra[@]}" workers=4
     mlflow.enabled=false out=experiments/outputs/v1/$out
     export.tables=${EXPORT_TABLES:-/dev/shm/scratch/$out-s$seed/tables}
     export.figures=${EXPORT_FIGURES:-/dev/shm/scratch/$out-s$seed/figures}
     hydra.run.dir=/dev/shm/hydra/$out-s$seed)
if [ "$lock" != "-" ]; then flock /dev/shm/locks/$lock "${cmd[@]}" > /Capstone/logs/$out-s$seed.log 2>&1
else "${cmd[@]}" > /Capstone/logs/$out-s$seed.log 2>&1; fi
if [ $? -eq 0 ]; then echo "$(date +%T) OK   $out seed $seed"; else echo "$(date +%T) FAIL $out seed $seed"; fi
JOB
chmod +x /Capstone/job.sh
SEEDS="0 1 2 3 4 5 6 7 8 9"
{
  for s in $SEEDS; do echo "none $s all none seed$s"; done
  for a in $RESIM; do for s in $SEEDS; do echo "$a $s all $a -"; done; done
  for s in $SEEDS; do echo "templates_core $s [e2d,b5_explanation_only,upper_lightgbm] templates_core seed$s"; done
  for a in no_forgery no_graph role_onehot role_per_role learning_curve_10 learning_curve_25 learning_curve_50 window_1h window_24h window_7d; do
    for s in $SEEDS; do echo "$a $s [e2d] $a seed$s"; done; done
  for s in $SEEDS; do echo "none $s [e2d_ecod,e2d_copod] scorers seed$s"; done
} > /dev/shm/jobs.txt
echo "$(date) start: $(wc -l < /dev/shm/jobs.txt) jobs" >> $L/progress.log
xargs -P 10 -L 1 /Capstone/job.sh < /dev/shm/jobs.txt >> $L/progress.log 2>&1
echo "$(date) all seed jobs ended" >> $L/progress.log
# Combine the 10 seeds of every folder (finished seeds are reused, nothing is refitted).
ALL="[0,1,2,3,4,5,6,7,8,9]"
EXPORT_TABLES=paper/tables EXPORT_FIGURES=paper/figures /Capstone/job.sh none all all none - $ALL >> $L/progress.log
/Capstone/job.sh templates_core all "[e2d,b5_explanation_only,upper_lightgbm]" templates_core - $ALL >> $L/progress.log
for a in $RESIM; do /Capstone/job.sh $a all all $a - $ALL >> $L/progress.log; done
for a in no_forgery no_graph role_onehot role_per_role learning_curve_10 learning_curve_25 learning_curve_50 window_1h window_24h window_7d; do
  /Capstone/job.sh $a all "[e2d]" $a - $ALL >> $L/progress.log; done
/Capstone/job.sh none all "[e2d_ecod,e2d_copod]" scorers - $ALL >> $L/progress.log
.venv/bin/python -m e2d_experiments.report experiments/outputs/v1 --export paper/tables > $L/report.log 2>&1 && echo "$(date) report OK" >> $L/progress.log || echo "$(date) report FAIL" >> $L/progress.log
# Keep the small run records (manifests, audits, splits) on disk, then pack everything to download.
for d in /dev/shm/runs/*/v1*; do r=experiments/outputs/v1/records/$(basename $d); mkdir -p $r; cp $d/manifest.json $d/audit.json $d/splits.json $r/ 2>/dev/null; done
tar czf /Capstone/results-v1.tar.gz --exclude='*/runs' paper/tables paper/figures experiments/outputs/v1 /Capstone/logs 2>/dev/null
echo "$(date) DONE: /Capstone/results-v1.tar.gz $(du -h /Capstone/results-v1.tar.gz | cut -f1)" >> $L/progress.log
