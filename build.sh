#!/bin/sh

set -o pipefail

python -m build
pip uninstall a3driverdoctor -y
pip install dist/a3driverdoctor-0.1.0-py3-none-any.whl
