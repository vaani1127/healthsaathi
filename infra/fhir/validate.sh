#!/bin/sh
# Validate FHIR bundles with the HAPI FHIR validator against the NRCeS ABDM IG (P17).
# Runs the validator in a Java container, with no terminology server ("validated offline"):
# coded values are checked for structure and profile rules, not looked up in LOINC or SNOMED.
# Fails if any bundle has an error; warnings and notes are printed as counts.
#
#   sh infra/fhir/validate.sh DIR_WITH_BUNDLES
set -eu
VERSION=7.0.1
IG=ndhm.in#6.5.0
# pwd -W gives a Windows path under Git Bash, which Docker Desktop can mount.
winpwd() { pwd -W 2>/dev/null || pwd; }
ROOT=$(cd "$(dirname "$0")/../.." && winpwd)
CACHE="$ROOT/.cache"
JAR="validator_cli-$VERSION.jar"
DIR=$(cd "$1" && winpwd)

mkdir -p "$CACHE/fhir-packages"
if [ ! -f "$CACHE/$JAR" ]; then
  echo "downloading the HAPI FHIR validator $VERSION"
  curl -sSfL -o "$CACHE/$JAR" \
    "https://github.com/hapifhir/org.hl7.fhir.core/releases/download/$VERSION/validator_cli.jar"
fi

files=""
for bundle in "$DIR"/*.json; do
  files="$files /data/$(basename "$bundle")"
done
# shellcheck disable=SC2086
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$CACHE:/tools" -v "$CACHE/fhir-packages:/root/.fhir" -v "$DIR:/data:ro" \
  eclipse-temurin:21-jre \
  java -jar "/tools/$JAR" $files -version 4.0.1 -ig "$IG" -tx n/a > "$DIR/validation.txt" 2>&1 || true

grep -E '^-- /data/|^\*?(Success|FAILURE)' "$DIR/validation.txt" | paste - - | sed 's/^-- \/data\///; s/ -*\t/: /'
errors=$(grep -c '^  Error' "$DIR/validation.txt" || true)
checked=$(grep -cE '^\*?(Success|FAILURE)' "$DIR/validation.txt" || true)
echo "bundles checked: $checked, errors: $errors (full output in $DIR/validation.txt)"
if [ "$checked" -eq 0 ] || [ "$errors" -ne 0 ]; then
  grep '^  Error' "$DIR/validation.txt" | head -40
  exit 1
fi
