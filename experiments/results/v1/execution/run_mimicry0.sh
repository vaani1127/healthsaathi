#!/bin/bash
# mimicry_0 with the audit gate off (its data fails the separability audit); approved, reported as unaudited.
cd /Capstone/healthsaathi
export UV_CACHE_DIR=/dev/shm/uv-cache SIM_WORKERS=4 POLARS_MAX_THREADS=4 OMP_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 MKL_NUM_THREADS=4
run() {
  .venv/bin/python -m e2d_experiments.run data=v1 data.require_audit=false ablation=mimicry_0 "seeds=$1" workers=4 \
    mlflow.enabled=false out=experiments/outputs/v1/mimicry_0 \
    export.tables=/dev/shm/scratch/mimicry_0u-$2/tables export.figures=/dev/shm/scratch/mimicry_0u-$2/figures \
    hydra.run.dir=/dev/shm/hydra/mimicry_0u-$2 > /Capstone/logs/mimicry_0u-$2.log 2>&1 \
    && echo "$(date +%T) OK   mimicry_0 unaudited $2" || echo "$(date +%T) FAIL mimicry_0 unaudited $2"
}
for batch in "0 1 2 3 4" "5 6 7 8 9"; do
  for s in $batch; do run "[$s]" s$s & done; wait
done
run "[0,1,2,3,4,5,6,7,8,9]" all
echo "$(date +%T) mimicry_0 finished"
