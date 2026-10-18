#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_INC="${ROOT_DIR}/third_party/openusd/include"

if [[ -f "${DEST_INC}/pxr/usdImaging/usdImagingGL/engine.h" ]]; then
    echo "OpenUSD headers already staged at ${DEST_INC}"
    exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

echo "Downloading OpenUSD v25.08 headers..."
curl -fL -o "${TMP_DIR}/openusd-25.08.tar.gz" \
    "https://github.com/PixarAnimationStudios/OpenUSD/archive/refs/tags/v25.08.tar.gz"

mkdir -p "${TMP_DIR}/src"
tar -xzf "${TMP_DIR}/openusd-25.08.tar.gz" -C "${TMP_DIR}/src" --strip-components=1 OpenUSD-25.08/pxr

python3 - "${TMP_DIR}/src/pxr" << 'PY'
import sys, os
pxr_dir = sys.argv[1]

with open(os.path.join(pxr_dir, "pxr.h.in")) as f:
    s = f.read()
s = s.replace("@PXR_MAJOR_VERSION@", "0")
s = s.replace("@PXR_MINOR_VERSION@", "25")
s = s.replace("@PXR_PATCH_VERSION@", "8")
s = s.replace("#define PXR_VERSION @PXR_VERSION@",
              "#if defined(__cplusplus) && __has_include(<oneapi/tbb/version.h>)\n#include <oneapi/tbb/version.h>\n#endif\n\n#define PXR_VERSION 2508")
s = s.replace("@PXR_USE_NAMESPACES@", "1")
s = s.replace("@PXR_EXTERNAL_NAMESPACE@", "pxr")
s = s.replace("@PXR_INTERNAL_NAMESPACE@", "pxrInternal__aapl")
s = s.replace("@PXR_PYTHON_SUPPORT_ENABLED@", "0")
s = s.replace("@PXR_PREFER_SAFETY_OVER_SPEED@", "1")
with open(os.path.join(pxr_dir, "pxr.h"), "w") as f:
    f.write(s)

impl_in = os.path.join(pxr_dir, "base/work/impl.h.in")
with open(impl_in) as f:
    w = f.read()
w = w.replace("@PXR_WORK_IMPL_HEADER@", '"pxr/base/work/workTBB/impl.h"')
with open(os.path.join(pxr_dir, "base/work/impl.h"), "w") as f:
    f.write(w)

# Patch vt/array.h, usd/primFlags.h, and sdf/pool.h for macOS system /usr/lib/usd/libusd_ms.tbd
array_h = os.path.join(pxr_dir, "base/vt/array.h")
with open(array_h) as f:
    a = f.read()
a = a.replace(
    "#define VT_ARRAY_EXTERN_TMPL(unused, elem) \\\n    VT_API_TEMPLATE_CLASS(VtArray< VT_TYPE(elem) >);\nTF_PP_SEQ_FOR_EACH(VT_ARRAY_EXTERN_TMPL, ~, VT_SCALAR_VALUE_TYPES)",
    "#if !defined(__APPLE__)\n#define VT_ARRAY_EXTERN_TMPL(unused, elem) \\\n    VT_API_TEMPLATE_CLASS(VtArray< VT_TYPE(elem) >);\nTF_PP_SEQ_FOR_EACH(VT_ARRAY_EXTERN_TMPL, ~, VT_SCALAR_VALUE_TYPES)\n#endif"
)
with open(array_h, "w") as f:
    f.write(a)

prim_flags_h = os.path.join(pxr_dir, "usd/usd/primFlags.h")
with open(prim_flags_h) as f:
    pf = f.read()
pf = pf.replace(
    "USD_API extern const Usd_PrimFlagsConjunction UsdPrimDefaultPredicate;",
    "#if defined(__APPLE__)\nUSD_API const Usd_PrimFlagsConjunction &GetUsdPrimDefaultPredicate();\n#define UsdPrimDefaultPredicate (GetUsdPrimDefaultPredicate())\n#else\nUSD_API extern const Usd_PrimFlagsConjunction UsdPrimDefaultPredicate;\n#endif"
)
with open(prim_flags_h, "w") as f:
    f.write(pf)

pool_h = os.path.join(pxr_dir, "usd/sdf/pool.h")
with open(pool_h) as f:
    pl = f.read()
pl = pl.replace(
    "        inline char *GetPtr() const noexcept {\n\t    ARCH_PRAGMA_PUSH\n            ARCH_PRAGMA_MAYBE_UNINITIALIZED\n            return Sdf_Pool::_GetPtr(value & RegionMask, value >> RegionBits);\n            ARCH_PRAGMA_POP\n        }",
    "#if defined(__APPLE__)\n        char *GetPtr() const noexcept;\n#else\n        inline char *GetPtr() const noexcept {\n\t    ARCH_PRAGMA_PUSH\n            ARCH_PRAGMA_MAYBE_UNINITIALIZED\n            return Sdf_Pool::_GetPtr(value & RegionMask, value >> RegionBits);\n            ARCH_PRAGMA_POP\n        }\n#endif"
)
with open(pool_h, "w") as f:
    f.write(pl)
PY

mkdir -p "${DEST_INC}/pxr"
rsync -a --prune-empty-dirs --include="*/" --include="*.h" --exclude="*" \
    "${TMP_DIR}/src/pxr/" "${DEST_INC}/pxr/"

echo "Staged OpenUSD headers for macOS system libusd_ms.dylib at ${DEST_INC}"
