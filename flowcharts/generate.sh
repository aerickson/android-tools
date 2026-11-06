#!/usr/bin/env bash

set -e

# cleanup
# rm *.png *.svg

# generate
for file in ./*.dot
do
  basename=$(basename -s .dot "$file")
  set -x
  dot -Tsvg "$basename".dot -o "$basename".svg
  dot -Tpng "$basename".dot -o "$basename".png
  set +x
done
