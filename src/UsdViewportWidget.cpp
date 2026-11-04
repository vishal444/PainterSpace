#include "pxr/pxr.h"
#include "pxr/base/gf/bbox3d.h"
#include "pxr/base/gf/camera.h"
#include "pxr/base/gf/frustum.h"
#include "pxr/base/gf/half.h"
#include "pxr/base/gf/matrix4d.h"
#include "pxr/base/gf/range3d.h"
#include "pxr/base/gf/ray.h"
#include "pxr/base/gf/rotation.h"
#include "pxr/base/gf/vec2d.h"
#include "pxr/base/gf/vec2f.h"
#include "pxr/base/gf/vec2i.h"
#include "pxr/base/gf/vec3d.h"
#include "pxr/base/gf/vec3f.h"
#include "pxr/base/gf/vec4d.h"
#include "pxr/base/gf/vec4f.h"
#include "pxr/base/vt/array.h"
#include "pxr/base/vt/value.h"
#include "pxr/imaging/cameraUtil/framing.h"
#include "pxr/imaging/glf/simpleLight.h"
#include "pxr/imaging/glf/simpleMaterial.h"
#include "pxr/imaging/hd/renderBuffer.h"
#include "pxr/imaging/hd/tokens.h"
#include "pxr/imaging/hdx/tokens.h"
#include "pxr/usd/sdf/copyUtils.h"
#include "pxr/usd/sdf/layer.h"
#include "pxr/usd/sdf/path.h"
#include "pxr/usd/sdf/primSpec.h"
#include "pxr/usd/usd/attribute.h"
#include "pxr/usd/usd/editContext.h"
#include "pxr/usd/usd/editTarget.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usd/primRange.h"
#include "pxr/usd/usd/references.h"
#include "pxr/usd/usd/stage.h"
#include "pxr/usd/usd/timeCode.h"
#include "pxr/usd/usdGeom/basisCurves.h"
#include "pxr/usd/usdGeom/camera.h"
#include "pxr/usd/usdGeom/cone.h"
#include "pxr/usd/usdGeom/cube.h"
#include "pxr/usd/usdGeom/cylinder.h"
#include "pxr/usd/usdGeom/gprim.h"
#include "pxr/usd/usdGeom/imageable.h"
#include "pxr/usd/usdGeom/metrics.h"
#include "pxr/usd/usdGeom/primvarsAPI.h"
#include "pxr/usd/usdGeom/scope.h"
#include "pxr/usd/usdGeom/sphere.h"
#include "pxr/usd/usdGeom/tokens.h"
#include "pxr/usd/usdGeom/xform.h"
#include "pxr/usd/usdGeom/xformCommonAPI.h"
#include "pxr/usd/usdGeom/xformOp.h"
#include "pxr/usd/usdGeom/xformable.h"
#include "pxr/usd/usdLux/distantLight.h"
#include "pxr/usd/usdLux/domeLight.h"
#include "pxr/usd/usdLux/sphereLight.h"
#include "pxr/usd/usdSkel/root.h"
#include "pxr/usdImaging/usdImagingGL/engine.h"
#include "pxr/usdImaging/usdImagingGL/renderParams.h"

#include "UsdViewportWidget.h"
#include "CameraTools.h"

#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QHash>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QLabel>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QProcess>
#include <QPushButton>
#include <QResizeEvent>
#include <QSaveFile>
#include <QSet>
#include <QRegularExpression>
#include <QSignalBlocker>
#include <QSlider>
#include <QTimer>
#include <QVBoxLayout>
#include <QVector2D>
#include <QOpenGLContext>
#include <QWheelEvent>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonArray>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <memory>
#include <utility>

#if defined(__APPLE__)
PXR_NAMESPACE_OPEN_SCOPE
struct USDInitializeConfiguration {
    alignas(8) unsigned char storage[64]{};
    USDInitializeConfiguration();
    ~USDInitializeConfiguration();
    void AddEntitlement(const std::string&);
};
void usd_initialize(const USDInitializeConfiguration&);
PXR_NAMESPACE_CLOSE_SCOPE
#endif

PXR_NAMESPACE_USING_DIRECTIVE

namespace {

// Playback redraws every frame; a smaller Hydra buffer keeps dense skinned
// scenes responsive. Paused editing and explicit captures retain full size.
constexpr double kPlaybackPreviewScale = 0.75;

// Free-view zoom stays inside its projection's clipping range. The dolly
// gesture can travel beyond the orbit point without changing this distance.
constexpr double kFreeViewNearClip = 0.1;
constexpr double kFreeViewFarClip = 1000.0;
constexpr double kZoomFactorPerStep = 0.88;
constexpr double kZoomDragPixelsPerStep = 100.0;

void ensureSystemUsdInitialized() {
    static bool initialized = false;
    if (initialized) return;
    initialized = true;
#if defined(__APPLE__)
    USDInitializeConfiguration cfg;
    cfg.AddEntitlement("com.apple.private.usd.enableusdgltf");
    cfg.AddEntitlement("com.apple.private.usd.enableadobefileformats");
    usd_initialize(cfg);
#endif
}

// Precomputed 16-bit half-float to sRGB 8-bit lookup table (65,536 entries)
// Converts linear Float16 AOV buffers to sRGB RGBA8888 in < 0.3 ms.
const uint8_t* halfToSrgbLut() {
    static uint8_t lut[65536];
    static bool initialized = false;
    if (!initialized) {
        for (int i = 0; i < 65536; ++i) {
            GfHalf h;
            h.setBits(static_cast<uint16_t>(i));
            float v = static_cast<float>(h);
            if (!std::isfinite(v) || v <= 0.0f) {
                lut[i] = 0;
            } else if (v >= 1.0f) {
                lut[i] = 255;
            } else {
                float srgb = std::pow(v, 1.0f / 2.2f);
                lut[i] = static_cast<uint8_t>(std::clamp(srgb * 255.0f + 0.5f, 0.0f, 255.0f));
            }
        }
        initialized = true;
    }
    return lut;
}

QMatrix4x4 gfMatrixToQMatrix(const GfMatrix4d& m) {
    // OpenUSD GfMatrix4d uses row-vector convention (v' = v * M).
    // Qt QMatrix4x4 uses column-vector convention (v' = M_qt * v), which is the transpose.
    QMatrix4x4 qm;
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            qm(r, c) = static_cast<float>(m[c][r]);
        }
    }
    return qm;
}

GfMatrix4d qMatrixToGfMatrix(const QMatrix4x4& qm) {
    GfMatrix4d m;
    for (int r = 0; r < 4; ++r) {
        for (int c = 0; c < 4; ++c) {
            m[r][c] = static_cast<double>(qm(c, r));
        }
    }
    return m;
}

// Resolves a picked gprim path (e.g. /World/Hero/Armature/Beta_Surface/Beta_Surface)
// to its top-level editable object prim under /World (e.g. /World/Hero).
SdfPath resolveEditablePrimPath(const UsdStageRefPtr& stage, const SdfPath& rawPath) {
    if (!stage || rawPath.IsEmpty()) return SdfPath();
    if (rawPath.HasPrefix(SdfPath("/World/Paint"))) {
        return rawPath;
    }
    SdfPath worldPath("/World");
    if (rawPath.HasPrefix(worldPath) && rawPath != worldPath) {
        SdfPath cur = rawPath;
        while (!cur.IsEmpty() && cur.GetParentPath() != worldPath) {
            cur = cur.GetParentPath();
        }
        if (!cur.IsEmpty()) return cur;
    }
    return rawPath;
}

void collectEditedPrimsInSpec(const SdfPrimSpecHandle& spec, QStringList& outPaths) {
    if (!spec) return;
    const SdfPath p = spec->GetPath();
    if (p.IsPrimPath() && p != SdfPath::AbsoluteRootPath() && p != SdfPath("/World")) {
        const bool hasOpinions = !spec->GetAttributes().empty() ||
                                 spec->GetSpecifier() == SdfSpecifierDef ||
                                 spec->HasInfo(SdfFieldKeys->References) ||
                                 spec->HasInfo(SdfFieldKeys->Active);
        if (hasOpinions) {
            outPaths.append(QString::fromStdString(p.GetString()));
        }
    }
    for (const SdfPrimSpecHandle& child : spec->GetNameChildren()) {
        collectEditedPrimsInSpec(child, outPaths);
    }
}

// A manual asset reference can survive shot generation as a prepended session
// reference. If the generated shot already references that same asset, USD
// composes both references and draws two copies under one actor prim.
bool reconcileGeneratedActorOpinions(const SdfPrimSpecHandle& sessionPrim,
                                     const SdfLayerRefPtr& shotLayer,
                                     const SdfLayerRefPtr& userLayer) {
    if (!sessionPrim || !shotLayer) return false;
    bool changed = false;
    const SdfPrimSpecHandle shotPrim = shotLayer->GetPrimAtPath(sessionPrim->GetPath());
    if (shotPrim && shotPrim->GetCustomData().count("actor_id")) {
        const auto shotRefs = shotPrim->GetReferenceList().GetAddedOrExplicitItems();
        const auto manualRefs = sessionPrim->GetReferenceList().GetPrependedItems();
        if (!shotRefs.empty() && !manualRefs.empty()) {
            auto assetWithoutExtension = [](const SdfReference& ref) {
                const QString path = QString::fromStdString(ref.GetAssetPath());
                const QFileInfo info(path);
                return info.path() + "/" + info.completeBaseName();
            };
            const bool allRedundant = std::all_of(manualRefs.begin(), manualRefs.end(),
                [&](const SdfReference& manual) {
                    return std::any_of(shotRefs.begin(), shotRefs.end(),
                        [&](const SdfReference& generated) {
                            return assetWithoutExtension(manual) == assetWithoutExtension(generated);
                        });
                });
            if (allRedundant) {
                sessionPrim->GetReferenceList().ClearEdits();
                changed = true;
            }
        }
        // A manually placed actor has a static transform in user_edits.usda.
        // Once the generated shot animates that same prim, the stronger static
        // value and xformOpOrder would hide every generated position and heading.
        bool generatedMotion = false;
        for (const auto& attr : shotPrim->GetAttributes()) {
            if (attr->GetName().rfind("xformOp:", 0) == 0 &&
                shotLayer->GetNumTimeSamplesForPath(attr->GetPath()) > 1) {
                generatedMotion = true;
                break;
            }
        }
        bool manualMotion = false;
        for (const auto& attr : sessionPrim->GetAttributes()) {
            if (attr->GetName().rfind("xformOp:", 0) == 0 &&
                userLayer->GetNumTimeSamplesForPath(attr->GetPath()) > 1) {
                manualMotion = true;
                break;
            }
        }
        if (generatedMotion && !manualMotion) {
            std::vector<SdfPropertySpecHandle> staticTransformOpinions;
            for (const auto& attr : sessionPrim->GetAttributes()) {
                if (attr->GetName().rfind("xformOp:", 0) == 0 ||
                    attr->GetName() == "xformOpOrder") {
                    staticTransformOpinions.push_back(attr);
                }
            }
            for (const auto& attr : staticTransformOpinions) {
                sessionPrim->RemoveProperty(attr);
                changed = true;
            }
        }
    }
    for (const SdfPrimSpecHandle& child : sessionPrim->GetNameChildren()) {
        changed = reconcileGeneratedActorOpinions(child, shotLayer, userLayer) || changed;
    }
    return changed;
}

// IMPORTANT (ABI safety): PainterSpace compiles against OpenUSD 25.08 headers staged in
// third_party/openusd, but links against Apple's private system build (/usr/lib/usd/libusd_ms).
// Those two do NOT agree on the private member layout of every class. UsdGeomBBoxCache is one
// that differs: its constructor runs inside libusd_ms (Apple layout) while its destructor is
// inlined from our headers (25.08 layout). Constructing one on our stack therefore corrupts
// memory and crashes in ~UsdGeomBBoxCache (EXC_BAD_ACCESS in TfToken::_RemoveRef) — this was
// the real cause of the "click an asset -> crash" bug.
//
// UsdGeomImageable::ComputeWorldBound() builds and destroys its BBoxCache entirely inside the
// library, and only returns a GfBBox3d (a plain value type with a stable layout), so it is safe.
// Never instantiate UsdGeomBBoxCache / UsdGeomXformCache directly in app code.
GfBBox3d computeWorldBoundSafe(const UsdPrim& prim, const UsdTimeCode& time) {
    if (!prim) return GfBBox3d();
    UsdGeomImageable imageable(prim);
    if (!imageable) return GfBBox3d();
    return imageable.ComputeWorldBound(time, UsdGeomTokens->default_, UsdGeomTokens->proxy, UsdGeomTokens->render);
}

// Grow the standard scene floor to include a newly placed asset. Keep its top
// height and thickness; author the override in the active edit layer.
void includeAssetInGround(const UsdStageRefPtr& stage, const UsdPrim& asset, const UsdTimeCode& time) {
    const UsdPrim ground = stage->GetPrimAtPath(SdfPath("/World/Ground"));
    const UsdGeomCube floor(stage->GetPrimAtPath(SdfPath("/World/Ground/Floor")));
    if (!ground || !floor) return;

    UsdGeomXformable groundXf(ground);
    bool resets = false;
    UsdGeomXformOp translateOp, scaleOp;
    for (const auto& op : groundXf.GetOrderedXformOps(&resets)) {
        if (op.GetOpType() == UsdGeomXformOp::TypeTranslate) translateOp = op;
        else if (op.GetOpType() == UsdGeomXformOp::TypeScale) scaleOp = op;
        else return; // A rotated or otherwise customized floor needs manual sizing.
    }
    if (!translateOp || !scaleOp || resets) return;

    const GfRange3d groundRange = computeWorldBoundSafe(ground, time).ComputeAlignedRange();
    const GfRange3d assetRange = computeWorldBoundSafe(asset, time).ComputeAlignedRange();
    if (groundRange.IsEmpty() || assetRange.IsEmpty()) return;

    const double minX = std::min(groundRange.GetMin()[0], assetRange.GetMin()[0]);
    const double maxX = std::max(groundRange.GetMax()[0], assetRange.GetMax()[0]);
    const double minZ = std::min(groundRange.GetMin()[2], assetRange.GetMin()[2]);
    const double maxZ = std::max(groundRange.GetMax()[2], assetRange.GetMax()[2]);
    if (minX == groundRange.GetMin()[0] && maxX == groundRange.GetMax()[0] &&
        minZ == groundRange.GetMin()[2] && maxZ == groundRange.GetMax()[2]) return;

    double cubeSize = 0.0;
    GfVec3d translation(0.0), scale(1.0);
    if (!floor.GetSizeAttr().Get(&cubeSize) || cubeSize <= 0.0) return;
    if (translateOp.GetPrecision() == UsdGeomXformOp::PrecisionFloat) {
        GfVec3f value;
        if (!translateOp.Get(&value, time)) return;
        translation = GfVec3d(value);
    } else if (!translateOp.Get(&translation, time)) return;
    if (scaleOp.GetPrecision() == UsdGeomXformOp::PrecisionFloat) {
        GfVec3f value;
        if (!scaleOp.Get(&value, time)) return;
        scale = GfVec3d(value);
    } else if (!scaleOp.Get(&scale, time)) return;
    const GfVec3d newTranslation((minX + maxX) / 2.0, translation[1], (minZ + maxZ) / 2.0);
    const GfVec3d newScale((maxX - minX) / cubeSize, scale[1], (maxZ - minZ) / cubeSize);
    if (translateOp.GetPrecision() == UsdGeomXformOp::PrecisionFloat)
        translateOp.Set(GfVec3f(newTranslation));
    else translateOp.Set(newTranslation);
    if (scaleOp.GetPrecision() == UsdGeomXformOp::PrecisionFloat)
        scaleOp.Set(GfVec3f(newScale));
    else scaleOp.Set(newScale);
}

} // namespace

struct UsdViewportWidget::Impl {
    UsdStageRefPtr stage;
    SdfLayerRefPtr userEditsLayer;
    std::unique_ptr<UsdImagingGLEngine> engine;

    QString stagePath;
    QString userEditsPath;
    bool isSceneStage = false;
    bool userEditsEnabled = true;

    // Timeline state
    double startFrame = 0.0;
    double endFrame = 120.0;
    double currentFrame = 0.0;
    double fps = 24.0;
    bool playing = false;
    QTimer* playTimer = nullptr;

    // Camera state
    bool useShotCam = false;
    QString shotCamPath = QStringLiteral("/World/ShotCam");
    QJsonObject cameraDocument;
    QJsonObject directorBrief;
    QStringList lintPaths;
    ViewMode viewMode = ViewMode::World;
    std::unique_ptr<CameraToolController> cameraTools;
    QTimer* rigBakeDebounceTimer = nullptr;
    QTimer* cameraPreviewTimer = nullptr;
    QProcess* cameraPreviewProcess = nullptr;
    QPushButton* viewModeButton = nullptr;
    QComboBox* shotSizeCombo = nullptr;
    QSlider* zoomSlider = nullptr;
    QLabel* zoomLabel = nullptr;
    QWidget* navigationBar = nullptr;
    bool fixedOrbitCenter = true;
    double yaw = 0.45;
    double pitch = 0.32;
    double distance = 12.0;
    QVector3D target = QVector3D(0.0f, 1.6f, -2.0f);

    // Interaction state
    PaintingTool activeTool = PaintingTool::Select;
    QString selectedPrimPath;
    bool selectedAimTarget = false;
    bool orbiting = false;
    bool panning = false;
    bool zooming = false;
    bool dollying = false;
    bool draggingGizmo = false;
    bool draggingObjectGround = false;
    bool dragUndoCaptured = false;
    bool draggingAimTarget = false;
    bool paintingStroke = false;
    int activeGizmoAxis = -1; // 0=X, 1=Y, 2=Z
    QPointF lastMousePos;
    QVector3D dragStartPrimPos;
    QVector3D dragStartAimPos;
    QVector3D dragStartWorldHit;
    QVector<QVector3D> currentStrokePoints;
    int nextStrokeIndex = 1;

    // General Asset Movement Paths (Characters, Vehicles, Animals, and Camera)
    bool drawPathActive = false;
    QString drawingPrimPath;
    QHash<QString, QVector<QVector3D>> primPaths;
    QSet<QString> explicitlyDrawnPrimPaths;
    QString lastDrawnPrimPath;
    QHash<QString, QColor> primPathColors;
    QVector<QVector3D> activePrimStroke;
    bool isDrawingPrimStroke = false;

    // Visual settings
    QColor backgroundColor = QColor("#262f38");
    QColor brushColor = QColor("#c85c4b");
    double brushSize = 12.0;
    double brushOpacity = 1.0;
    bool showGroundGrid = true;

    // Cached frame & performance
    QImage cachedFrame;
    bool hydraDirty = true;
    double lastRenderMs = 0.0;
    mutable QHash<QString, GfBBox3d> worldBoundCache;
    mutable double worldBoundCacheFrame = -1e9;
    bool pendingDragSave = false;

    GfBBox3d cachedWorldBound(const UsdPrim& prim) const {
        if (!prim) return GfBBox3d();
        if (std::abs(worldBoundCacheFrame - currentFrame) > 1e-4) {
            worldBoundCache.clear();
            worldBoundCacheFrame = currentFrame;
        }
        const QString key = QString::fromStdString(prim.GetPath().GetString());
        auto it = worldBoundCache.find(key);
        if (it != worldBoundCache.end()) {
            return it.value();
        }
        GfBBox3d bbox = computeWorldBoundSafe(prim, UsdTimeCode(currentFrame));
        worldBoundCache.insert(key, bbox);
        return bbox;
    }

    void invalidateBound(const QString& primPath) {
        if (primPath.isEmpty()) {
            worldBoundCache.clear();
        } else {
            worldBoundCache.remove(primPath);
        }
    }

    struct ColliderBox {
        GfVec3d center;
        GfVec3d size;
    };
    mutable QHash<QString, QVector<ColliderBox>> manifestColliders;
    mutable bool manifestCollidersLoaded = false;

    void loadManifestCollidersIfNeeded() const {
        if (manifestCollidersLoaded || !stage) return;
        manifestCollidersLoaded = true;
        manifestColliders.clear();

        std::string stagePath = stage->GetRootLayer()->GetRealPath();
        if (stagePath.empty()) stagePath = stage->GetRootLayer()->GetIdentifier();
        QDir curDir(QFileInfo(QString::fromStdString(stagePath)).absoluteDir());
        QString manifestPath;
        for (int i = 0; i < 5; ++i) {
            if (QFileInfo::exists(curDir.filePath("manifest.json"))) {
                manifestPath = curDir.filePath("manifest.json");
                break;
            }
            if (!curDir.cdUp()) break;
        }
        if (manifestPath.isEmpty()) return;

        QFile f(manifestPath);
        if (!f.open(QIODevice::ReadOnly)) return;
        QJsonDocument doc = QJsonDocument::fromJson(f.readAll());
        if (!doc.isObject()) return;
        QJsonObject root = doc.object();
        QJsonArray assets = root.value("assets").toArray();
        for (const QJsonValue& val : assets) {
            QJsonObject a = val.toObject();
            QString id = a.value("id").toString();
            QJsonObject col = a.value("collision").toObject();
            QJsonArray shapes = col.value("shapes").toArray();
            if (shapes.isEmpty()) continue;
            QVector<ColliderBox> boxes;
            for (const QJsonValue& sVal : shapes) {
                QJsonObject s = sVal.toObject();
                QJsonArray cArr = s.value("center").toArray();
                QJsonArray szArr = s.value("size").toArray();
                if (cArr.size() == 3 && szArr.size() == 3) {
                    boxes.push_back({
                        GfVec3d(cArr[0].toDouble(), cArr[1].toDouble(), cArr[2].toDouble()),
                        GfVec3d(szArr[0].toDouble(), szArr[1].toDouble(), szArr[2].toDouble())
                    });
                }
            }
            if (!boxes.isEmpty()) {
                manifestColliders.insert(id, boxes);
                QString p = a.value("path").toString();
                if (!p.isEmpty()) {
                    manifestColliders.insert(QFileInfo(p).completeBaseName(), boxes);
                }
            }
        }
    }

    // Status banner (Phase 2)
    QString statusBannerText;
    bool statusBannerError = false;
    bool statusBannerBusy = false;

    // Live reload file watcher (Phase 2)
    QFileSystemWatcher* fileWatcher = nullptr;
    bool suppressWatcherReload = false;

    // Undo / Redo history of user_edits.usda text (Phase 3)
    QStringList undoStack;
    QStringList redoStack;

    // Overlay HUD widgets
    QWidget* bottomBar = nullptr;
    QPushButton* playButton = nullptr;
    QSlider* timelineSlider = nullptr;
    QLabel* frameLabel = nullptr;
    QComboBox* cameraCombo = nullptr;
    QLabel* userEditsBadge = nullptr;
    QCheckBox* userEditsCheck = nullptr;
    QPushButton* resetEditsButton = nullptr;
    QPushButton* undoButton = nullptr;
};

UsdViewportWidget::UsdViewportWidget(QWidget* parent)
    : QWidget(parent), myImpl(std::make_unique<Impl>()) {
    ensureSystemUsdInitialized();
    setFocusPolicy(Qt::StrongFocus);
    setMouseTracking(true);
    setMinimumSize(480, 320);

    myImpl->playTimer = new QTimer(this);
    myImpl->cameraTools = std::make_unique<CameraToolController>(this);
    myImpl->rigBakeDebounceTimer = new QTimer(this);
    myImpl->rigBakeDebounceTimer->setSingleShot(true);
    connect(myImpl->cameraTools.get(), &CameraToolController::rigChanged, this, [this]() {
        if (myImpl->suppressWatcherReload) return;
        if (myImpl->rigBakeDebounceTimer) {
            myImpl->rigBakeDebounceTimer->start(100);
        }
        myImpl->hydraDirty = true;
        update();
    });
    connect(myImpl->rigBakeDebounceTimer, &QTimer::timeout, this, [this]() {
        bakeCurrentCameraRig();
    });
    myImpl->cameraPreviewTimer = new QTimer(this); myImpl->cameraPreviewTimer->setSingleShot(true); myImpl->cameraPreviewTimer->setInterval(250);
    connect(myImpl->cameraPreviewTimer, &QTimer::timeout, this, [this]() {
        if (myImpl->cameraPreviewProcess) { myImpl->cameraPreviewTimer->start(); return; }
        const auto dir = QFileInfo(myImpl->stagePath).dir();
        if (!QFileInfo::exists(dir.filePath("shot.json")) || !QFileInfo::exists(dir.filePath("camera_rig.json"))) return;
        QDir candidate = dir;
        while (!QFileInfo::exists(candidate.filePath("tools/shot_library/compile_shot.py")) && candidate.cdUp()) {}
        const QString root = QFileInfo::exists(candidate.filePath("tools/shot_library/compile_shot.py")) ? candidate.absolutePath() : QDir::currentPath();
        auto* process = new QProcess(this); myImpl->cameraPreviewProcess = process;
        const QString currentStage = myImpl->stagePath;
        connect(process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, process, currentStage, dir](int code, QProcess::ExitStatus) {
            myImpl->cameraPreviewProcess = nullptr; process->deleteLater();
            if (code != 0 || myImpl->stagePath != currentStage) return;
            const auto doc = myImpl->cameraDocument; const auto cameras = doc["cameras"].toObject();
            for (auto it = cameras.begin(); it != cameras.end(); ++it) {
                auto layer = SdfLayer::FindOrOpen(dir.filePath("cam_" + it.key() + ".usda").toStdString()); if (layer) layer->Reload();
            }
            setCameraDocument(myImpl->cameraDocument);
            myImpl->hydraDirty = true; update();
        });
        connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError e) { if (e == QProcess::FailedToStart) { myImpl->cameraPreviewProcess = nullptr; process->deleteLater(); } });
        process->start(QDir(root).filePath(".venv/bin/python"), {QDir(root).filePath("tools/shot_library/compile_shot.py"), "--out", dir.filePath("shot.usda"), "--camera-preview"});
    });
    connect(myImpl->playTimer, &QTimer::timeout, this, [this]() {
        if (!myImpl->stage) return;
        double next = myImpl->currentFrame + 1.0;
        if (next > myImpl->endFrame) {
            next = myImpl->startFrame;
        }
        setCurrentFrame(next);
    });

    myImpl->fileWatcher = new QFileSystemWatcher(this);
    connect(myImpl->fileWatcher, &QFileSystemWatcher::fileChanged, this, [this](const QString& changedPath) {
        if (myImpl->suppressWatcherReload) return;
        if (changedPath.endsWith("camera_rig.json")) {
            QFile file(changedPath);
            if (file.open(QIODevice::ReadOnly)) setCameraDocument(QJsonDocument::fromJson(file.readAll()).object());
            myImpl->cameraPreviewTimer->start(); syncWatchedFiles(); return;
        }
        // Re-add path in case atomic save replaced the inode
        QTimer::singleShot(80, this, [this, changedPath]() {
            syncWatchedFiles();
            if (!myImpl->suppressWatcherReload && myImpl->stage) {
                reloadStage();
                emit statusMessageChanged(QString("Live reloaded USD change from %1")
                                              .arg(QFileInfo(changedPath).fileName()));
            }
        });
    });

    setupOverlayControls();
}

UsdViewportWidget::~UsdViewportWidget() = default;

void UsdViewportWidget::setupOverlayControls() {
    QVBoxLayout* mainLayout = new QVBoxLayout(this);
    mainLayout->setContentsMargins(10, 10, 10, 10);
    mainLayout->addStretch(1);

    myImpl->bottomBar = new QWidget(this);
    myImpl->bottomBar->setObjectName("usdBottomBar");
    myImpl->bottomBar->setStyleSheet(
        "QWidget#usdBottomBar, QWidget#usdNavigationBar { background: rgba(21, 25, 30, 225); border: 1px solid #343e4b; border-radius: 8px; }"
        "QPushButton { background: #242c35; color: #e3e8ed; border: 1px solid #3c4856; border-radius: 5px; padding: 4px 10px; font-size: 11px; font-weight: 600; }"
        "QPushButton:hover { background: #313d4b; border-color: #73b5da; }"
        "QComboBox { background: #20262e; color: #e3e8ed; border: 1px solid #3c4856; border-radius: 5px; padding: 3px 8px; font-size: 11px; min-height: 22px; }"
        "QLabel { color: #cfd8e2; font-size: 11px; }"
        "QCheckBox { color: #cfd8e2; font-size: 11px; spacing: 5px; }");

    myImpl->navigationBar = new QWidget(this);
    myImpl->navigationBar->setObjectName("usdNavigationBar");
    myImpl->navigationBar->setStyleSheet(myImpl->bottomBar->styleSheet());
    auto* navigation = new QHBoxLayout(myImpl->navigationBar);
    navigation->setContentsMargins(8, 6, 8, 6);
    for (const auto& control : {std::pair<const char*, double>{"Zoom In", 1.0},
                               std::pair<const char*, double>{"Zoom Out", -1.0}}) {
        auto* button = new QPushButton(QString::fromLatin1(control.first), myImpl->navigationBar);
        button->setAccessibleName(button->text());
        button->setToolTip(button->text() + " around the editor's center");
        button->setCursor(Qt::PointingHandCursor);
        button->setFocusPolicy(Qt::NoFocus);
        button->setAutoRepeat(true);
        connect(button, &QPushButton::clicked, this, [this, steps = control.second]() {
            zoomFreeCamera(steps, QPointF(width() * 0.5, height() * 0.5), false, false);
            setFocus(Qt::MouseFocusReason);
        });
        navigation->addWidget(button);
    }
    auto* fixedCenter = new QCheckBox("Fixed Center", myImpl->navigationBar);
    fixedCenter->setChecked(myImpl->fixedOrbitCenter);
    fixedCenter->setFocusPolicy(Qt::NoFocus);
    fixedCenter->setToolTip("Keep the orbit center and vertical axis fixed while rotating, tilting and zooming. "
                           "Turn off to pan, dolly or zoom toward the pointer. F deliberately centers on the selection.");
    connect(fixedCenter, &QCheckBox::toggled, this, [this](bool checked) {
        myImpl->fixedOrbitCenter = checked;
        myImpl->panning = false;
        update();
    });
    navigation->addWidget(fixedCenter);
    auto* navigationRow = new QHBoxLayout;
    navigationRow->addStretch();
    navigationRow->addWidget(myImpl->navigationBar);
    navigationRow->addStretch();
    mainLayout->insertLayout(0, navigationRow);

    QHBoxLayout* barLayout = new QHBoxLayout(myImpl->bottomBar);
    barLayout->setContentsMargins(10, 6, 10, 6);
    barLayout->setSpacing(8);

    myImpl->playButton = new QPushButton("▶ Play", myImpl->bottomBar);
    myImpl->playButton->setCursor(Qt::PointingHandCursor);
    connect(myImpl->playButton, &QPushButton::clicked, this, [this]() {
        setPlaying(!myImpl->playing);
    });
    barLayout->addWidget(myImpl->playButton);

    myImpl->timelineSlider = new QSlider(Qt::Horizontal, myImpl->bottomBar);
    myImpl->timelineSlider->setRange(0, 120);
    myImpl->timelineSlider->setMinimumWidth(140);
    connect(myImpl->timelineSlider, &QSlider::valueChanged, this, [this](int value) {
        if (myImpl->playing) setPlaying(false);
        setCurrentFrame(static_cast<double>(value));
    });
    barLayout->addWidget(myImpl->timelineSlider, 1);

    myImpl->frameLabel = new QLabel("Frame 0 / 120 (24 fps)", myImpl->bottomBar);
    myImpl->frameLabel->setMinimumWidth(125);
    barLayout->addWidget(myImpl->frameLabel);

    myImpl->viewModeButton = new QPushButton(myImpl->viewMode == ViewMode::CameraViewfinder ? "🌐 Switch to 3D World (Tab)" : "🎥 Switch to Viewfinder (Tab)", myImpl->bottomBar);
    myImpl->viewModeButton->setToolTip("Toggle World 3D editing view and Camera Viewfinder (Tab)");
    myImpl->viewModeButton->setCursor(Qt::PointingHandCursor);
    connect(myImpl->viewModeButton, &QPushButton::clicked, this, &UsdViewportWidget::toggleViewMode);
    barLayout->addWidget(myImpl->viewModeButton);

    myImpl->shotSizeCombo = new QComboBox(myImpl->bottomBar);
    myImpl->shotSizeCombo->addItem("Shot: Wide", static_cast<int>(CameraShotSize::Wide));
    myImpl->shotSizeCombo->addItem("Shot: Medium", static_cast<int>(CameraShotSize::Medium));
    myImpl->shotSizeCombo->addItem("Shot: Medium Close", static_cast<int>(CameraShotSize::MediumCloseUp));
    myImpl->shotSizeCombo->addItem("Shot: Close-Up", static_cast<int>(CameraShotSize::CloseUp));
    myImpl->shotSizeCombo->addItem("Shot: Extreme Wide", static_cast<int>(CameraShotSize::ExtremeWide));
    myImpl->shotSizeCombo->addItem("Shot: Extreme Close", static_cast<int>(CameraShotSize::ExtremeCloseUp));
    myImpl->shotSizeCombo->setCurrentIndex(1); // Medium default
    connect(myImpl->shotSizeCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int idx) {
        setShotSize(static_cast<CameraShotSize>(myImpl->shotSizeCombo->itemData(idx).toInt()));
    });
    barLayout->addWidget(myImpl->shotSizeCombo);

    myImpl->zoomSlider = new QSlider(Qt::Horizontal, myImpl->bottomBar);
    myImpl->zoomSlider->setRange(12, 200);
    myImpl->zoomSlider->setValue(35);
    myImpl->zoomSlider->setFixedWidth(80);
    myImpl->zoomSlider->setToolTip("Lens Zoom (Focal Length mm)");
    connect(myImpl->zoomSlider, &QSlider::valueChanged, this, [this](int val) {
        setCameraFocalLength(static_cast<double>(val));
    });
    barLayout->addWidget(myImpl->zoomSlider);

    myImpl->zoomLabel = new QLabel("35 mm", myImpl->bottomBar);
    myImpl->zoomLabel->setFixedWidth(46);
    barLayout->addWidget(myImpl->zoomLabel);

    myImpl->cameraCombo = new QComboBox(myImpl->bottomBar);
    myImpl->cameraCombo->addItem("Camera: ShotCam", true);
    myImpl->cameraCombo->addItem("Camera: Free Orbit", false);
    myImpl->cameraCombo->setVisible(false);
    connect(myImpl->cameraCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int idx) {
        setUseShotCamera(myImpl->cameraCombo->itemData(idx).toBool());
    });


    myImpl->userEditsBadge = new QLabel("Your changes: 0", myImpl->bottomBar);
    myImpl->userEditsBadge->setStyleSheet(
        "background: #273849; color: #8ad0f5; border: 1px solid #416382; border-radius: 4px; padding: 3px 7px; font-weight: 700;");
    barLayout->addWidget(myImpl->userEditsBadge);

    myImpl->userEditsCheck = new QCheckBox("Apply", myImpl->bottomBar);
    myImpl->userEditsCheck->setChecked(true);
    myImpl->userEditsCheck->setToolTip("Toggle applying the separate user_edits.usda layer over shot.usda");
    connect(myImpl->userEditsCheck, &QCheckBox::toggled, this, &UsdViewportWidget::setUserEditsEnabled);
    barLayout->addWidget(myImpl->userEditsCheck);

    myImpl->undoButton = new QPushButton("↶ Undo", myImpl->bottomBar);
    myImpl->undoButton->setToolTip("Undo last change (Cmd+Z / Ctrl+Z)");
    myImpl->undoButton->setCursor(Qt::PointingHandCursor);
    connect(myImpl->undoButton, &QPushButton::clicked, this, &UsdViewportWidget::undo);
    barLayout->addWidget(myImpl->undoButton);

    myImpl->resetEditsButton = new QPushButton("Reset Edits", myImpl->bottomBar);
    myImpl->resetEditsButton->setToolTip("Discard manual overrides in user_edits.usda while keeping LLM shot.usda intact");
    myImpl->resetEditsButton->setCursor(Qt::PointingHandCursor);
    connect(myImpl->resetEditsButton, &QPushButton::clicked, this, [this]() {
        if (!myImpl->selectedPrimPath.isEmpty()) {
            clearSelectedPrimUserEdits();
        } else {
            clearAllUserEdits();
        }
    });
    barLayout->addWidget(myImpl->resetEditsButton);

    mainLayout->addWidget(myImpl->bottomBar);
    updateOverlayUi();
}

void UsdViewportWidget::updateOverlayUi() {
    if (!myImpl->bottomBar) return;
    {
        QSignalBlocker blockSlider(myImpl->timelineSlider);
        myImpl->timelineSlider->setRange(static_cast<int>(std::floor(myImpl->startFrame)),
                                         static_cast<int>(std::ceil(myImpl->endFrame)));
        myImpl->timelineSlider->setValue(static_cast<int>(std::round(myImpl->currentFrame)));
    }
    myImpl->playButton->setText(myImpl->playing ? "Ⅱ Pause" : "▶ Play");
    const double seconds = myImpl->fps > 0.0 ? (myImpl->currentFrame / myImpl->fps) : 0.0;
    myImpl->frameLabel->setText(QString("F %1/%2 (%3s)")
                                    .arg(static_cast<int>(std::round(myImpl->currentFrame)))
                                    .arg(static_cast<int>(std::round(myImpl->endFrame)))
                                    .arg(seconds, 0, 'f', 1));
    {
        QSignalBlocker blockCam(myImpl->cameraCombo);
        myImpl->cameraCombo->setCurrentIndex(myImpl->useShotCam ? 0 : 1);
    }
    if (myImpl->viewModeButton) {
        myImpl->viewModeButton->setText(myImpl->viewMode == ViewMode::CameraViewfinder ? "🌐 Switch to 3D World (Tab)" : "🎥 Switch to Viewfinder (Tab)");
        myImpl->viewModeButton->setStyleSheet(myImpl->viewMode == ViewMode::CameraViewfinder
            ? "background: #1d4ed8; color: #ffffff; border: 1px solid #60a5fa; font-weight: bold; border-radius: 5px; padding: 4px 10px;"
            : "");
    }
    if (myImpl->navigationBar) {
        myImpl->navigationBar->setVisible(myImpl->viewMode == ViewMode::World);
    }
    if (myImpl->shotSizeCombo && myImpl->cameraTools) {
        QSignalBlocker b(myImpl->shotSizeCombo);
        const int idx = myImpl->shotSizeCombo->findData(static_cast<int>(myImpl->cameraTools->rigData().shotSize));
        if (idx >= 0) myImpl->shotSizeCombo->setCurrentIndex(idx);
    }
    if (myImpl->zoomSlider && myImpl->cameraTools) {
        QSignalBlocker b(myImpl->zoomSlider);
        myImpl->zoomSlider->setValue(static_cast<int>(std::round(myImpl->cameraTools->rigData().focalStart)));
    }
    if (myImpl->zoomLabel && myImpl->cameraTools) {
        myImpl->zoomLabel->setText(QString("%1 mm").arg(static_cast<int>(std::round(myImpl->cameraTools->rigData().focalStart))));
    }
    const int editCount = userEditedPrimCount();
    const QStringList orphaned = orphanedUserEditPrims();
    if (!orphaned.isEmpty()) {
        myImpl->userEditsBadge->setText(QString("⚠ Your changes: %1 (%2 missing)").arg(editCount).arg(orphaned.size()));
        myImpl->userEditsBadge->setStyleSheet(
            "background: #4a2c1d; color: #f7b279; border: 1px solid #8c5233; border-radius: 4px; padding: 3px 7px; font-weight: 700;");
        myImpl->userEditsBadge->setToolTip("Warning: user_edits.usda overrides prim(s) not found in shot.usda: " + orphaned.join(", "));
    } else if (editCount > 0) {
        myImpl->userEditsBadge->setText(QString("✎ Your changes: %1").arg(editCount));
        myImpl->userEditsBadge->setStyleSheet(
            "background: #223b2e; color: #7ed9a6; border: 1px solid #35634b; border-radius: 4px; padding: 3px 7px; font-weight: 700;");
        myImpl->userEditsBadge->setToolTip(QString("Manual edits stored in %1 (shot.usda untouched)").arg(myImpl->userEditsPath));
    } else {
        myImpl->userEditsBadge->setText("Your changes: 0");
        myImpl->userEditsBadge->setStyleSheet(
            "background: #273849; color: #8ad0f5; border: 1px solid #416382; border-radius: 4px; padding: 3px 7px; font-weight: 700;");
        myImpl->userEditsBadge->setToolTip("All manual edits will be written to user_edits.usda");
    }
    {
        QSignalBlocker blockCheck(myImpl->userEditsCheck);
        myImpl->userEditsCheck->setChecked(myImpl->userEditsEnabled);
    }
    if (myImpl->undoButton) {
        myImpl->undoButton->setEnabled(canUndo());
    }
    myImpl->resetEditsButton->setEnabled(editCount > 0);
    myImpl->resetEditsButton->setText(myImpl->selectedPrimPath.isEmpty() ? "Reset All" : "Reset Prim");
}

bool UsdViewportWidget::openStage(const QString& stagePath, bool isSceneStage) {
    if (stagePath.isEmpty()) return false;
    const QFileInfo info(stagePath);
    if (!info.exists()) return false;

    const QString absStagePath = info.absoluteFilePath();
    const QString absUserEditsPath = info.dir().filePath("user_edits.usda");

    myImpl->suppressWatcherReload = true;
    if (!QFileInfo::exists(absUserEditsPath)) {
        QSaveFile initFile(absUserEditsPath);
        const QByteArray header = "#usda 1.0\n(\n    doc = \"PainterSpace manual user edits layer\"\n)\n\nover \"World\"\n{\n}\n";
        if (initFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
            initFile.write(header);
            initFile.commit();
        }
    }

    SdfLayerRefPtr rootLayer = SdfLayer::FindOrOpen(absStagePath.toStdString());
    if (!rootLayer) {
        myImpl->suppressWatcherReload = false;
        return false;
    }
    rootLayer->Reload();

    SdfLayerRefPtr userLayer = SdfLayer::FindOrOpen(absUserEditsPath.toStdString());
    if (userLayer) {
        userLayer->Reload();
        if (!isSceneStage && reconcileGeneratedActorOpinions(
                userLayer->GetPrimAtPath(SdfPath("/World")), rootLayer, userLayer)) {
            userLayer->Save();
        }
    }

    UsdStageRefPtr newStage = userLayer
        ? UsdStage::Open(rootLayer, userLayer, UsdStage::LoadAll)
        : UsdStage::Open(rootLayer, UsdStage::LoadAll);
    if (!newStage) {
        myImpl->suppressWatcherReload = false;
        return false;
    }

    myImpl->stage = newStage;
    myImpl->userEditsLayer = userLayer;
    myImpl->stagePath = absStagePath;
    myImpl->userEditsPath = absUserEditsPath;
    myImpl->isSceneStage = isSceneStage;
    myImpl->primPaths.clear();
    myImpl->explicitlyDrawnPrimPaths.clear();
    myImpl->lastDrawnPrimPath.clear();
    if (myImpl->userEditsLayer) {
        const auto metadata = myImpl->userEditsLayer->GetCustomLayerData();
        const auto savedPaths = metadata.find("director_actor_paths");
        if (savedPaths != metadata.end() && savedPaths->second.IsHolding<std::string>()) {
            const auto paths = QJsonDocument::fromJson(QByteArray::fromStdString(savedPaths->second.UncheckedGet<std::string>())).object();
            for (auto it = paths.begin(); it != paths.end(); ++it) {
                QVector<QVector3D> points;
                for (const auto& value : it.value().toArray()) {
                    const auto coords = value.toArray();
                    if (coords.size() == 3) points.append(QVector3D(coords[0].toDouble(), coords[1].toDouble(), coords[2].toDouble()));
                }
                myImpl->primPaths[it.key()] = points;
            }
        }
        const auto drawn = metadata.find("director_drawn_paths");
        if (drawn != metadata.end() && drawn->second.IsHolding<std::string>()) {
            const auto paths = QJsonDocument::fromJson(QByteArray::fromStdString(drawn->second.UncheckedGet<std::string>())).array();
            for (const auto& path : paths) myImpl->explicitlyDrawnPrimPaths.insert(path.toString());
        }
    }

    if (myImpl->userEditsLayer) {
        if (!myImpl->userEditsEnabled) {
            myImpl->stage->MuteLayer(myImpl->userEditsLayer->GetIdentifier());
        }
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    // Recreate engine for the newly opened stage
    myImpl->engine.reset();
    myImpl->manifestCollidersLoaded = false;
    myImpl->manifestColliders.clear();

    // Read stage timing metadata
    const double startTc = myImpl->stage->HasAuthoredTimeCodeRange() ? myImpl->stage->GetStartTimeCode() : 0.0;
    const double endTc = myImpl->stage->HasAuthoredTimeCodeRange() ? myImpl->stage->GetEndTimeCode() : 120.0;
    const double tcps = myImpl->stage->GetTimeCodesPerSecond();
    myImpl->startFrame = startTc;
    myImpl->endFrame = std::max(startTc, endTc);
    myImpl->fps = tcps > 0.0 ? tcps : 24.0;
    myImpl->currentFrame = startTc;
    myImpl->playTimer->setInterval(std::max(1, static_cast<int>(std::round(1000.0 / myImpl->fps))));

    // Detect primary camera on stage
    myImpl->shotCamPath = QStringLiteral("/World/ShotCam");
    UsdPrim shotCamPrim = myImpl->stage->GetPrimAtPath(SdfPath("/World/ShotCam"));
    if (!shotCamPrim || !shotCamPrim.IsA<UsdGeomCamera>()) {
        for (const UsdPrim& prim : myImpl->stage->Traverse()) {
            if (prim.IsA<UsdGeomCamera>() && !prim.GetPath().GetString().find("/World/ShotCam")) {
                myImpl->shotCamPath = QString::fromStdString(prim.GetPath().GetString());
                shotCamPrim = prim;
                break;
            }
        }
    }
    // Opening a stage should land in the editable 3D world. The camera viewfinder
    // remains available as an explicit toggle for framing and preview.
    myImpl->viewMode = ViewMode::World;
    myImpl->useShotCam = false;
    myImpl->cameraTools->setTool(CameraToolType::None);

    // Load camera rig if authored, or initialize from shot camera
    CameraRigData rig;
    if (loadCameraRigFromStage(myImpl->stage, rig)) {
        if (!rig.hasCustomLookAt) {
            QVector3D sceneMid(0.0f, 1.2f, 0.0f);
            UsdPrim worldPrim = myImpl->stage->GetPrimAtPath(SdfPath("/World"));
            if (worldPrim) {
                GfBBox3d bbox = myImpl->cachedWorldBound(worldPrim);
                GfRange3d range = bbox.ComputeAlignedRange();
                if (!range.IsEmpty()) {
                    GfVec3d m = range.GetMidpoint();
                    sceneMid = QVector3D(float(m[0]), std::max(0.5f, float(m[1])), float(m[2]));
                }
            }
            rig.lookAtPoint = sceneMid;
        }
        myImpl->cameraTools->setRigData(rig);
    } else if (shotCamPrim && shotCamPrim.IsA<UsdGeomCamera>() && !isSceneStage) {
        if (convertShotCamToRig(myImpl->stage, myImpl->startFrame, myImpl->endFrame, myImpl->fps, rig)) {
            if (!rig.hasCustomLookAt) {
                QVector3D sceneMid(0.0f, 1.2f, 0.0f);
                UsdPrim worldPrim = myImpl->stage->GetPrimAtPath(SdfPath("/World"));
                if (worldPrim) {
                    GfBBox3d bbox = myImpl->cachedWorldBound(worldPrim);
                    GfRange3d range = bbox.ComputeAlignedRange();
                    if (!range.IsEmpty()) {
                        GfVec3d m = range.GetMidpoint();
                        sceneMid = QVector3D(float(m[0]), std::max(0.5f, float(m[1])), float(m[2]));
                    }
                }
                rig.lookAtPoint = sceneMid;
            }
            myImpl->cameraTools->setRigData(rig);
        }
    }

    QFile cameraFile(info.dir().filePath("camera_rig.json"));
    myImpl->cameraDocument = {};
    if (!isSceneStage && cameraFile.open(QIODevice::ReadOnly)) {
        const QByteArray original = cameraFile.readAll();
        setCameraDocument(QJsonDocument::fromJson(original).object());
    }

    // Seed undo stack with initial user_edits.usda content
    myImpl->undoStack.clear();
    myImpl->redoStack.clear();
    if (myImpl->userEditsLayer) myImpl->undoStack.append(captureUndoState());

    if (!myImpl->cameraDocument.isEmpty()) myImpl->cameraPreviewTimer->start();
    syncWatchedFiles();
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    return true;
}

void UsdViewportWidget::reloadStage() {
    if (!myImpl->stage) return;
    myImpl->suppressWatcherReload = true;
    myImpl->stage->Reload();
    if (!myImpl->isSceneStage && myImpl->userEditsLayer &&
        reconcileGeneratedActorOpinions(myImpl->userEditsLayer->GetPrimAtPath(SdfPath("/World")),
                                        myImpl->stage->GetRootLayer(), myImpl->userEditsLayer)) {
        myImpl->userEditsLayer->Save();
    }
    if (myImpl->userEditsLayer) {
        if (!myImpl->userEditsEnabled) {
            myImpl->stage->MuteLayer(myImpl->userEditsLayer->GetIdentifier());
        } else {
            myImpl->stage->UnmuteLayer(myImpl->userEditsLayer->GetIdentifier());
        }
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    if (myImpl->stage->HasAuthoredTimeCodeRange()) {
        myImpl->startFrame = myImpl->stage->GetStartTimeCode();
        myImpl->endFrame = std::max(myImpl->startFrame, myImpl->stage->GetEndTimeCode());
        myImpl->currentFrame = std::clamp(myImpl->currentFrame, myImpl->startFrame, myImpl->endFrame);
    }
    const double tcps = myImpl->stage->GetTimeCodesPerSecond();
    if (tcps > 0.0) myImpl->fps = tcps;

    myImpl->engine.reset();
    myImpl->invalidateBound(QString());
    myImpl->suppressWatcherReload = false;
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::syncWatchedFiles() {
    if (!myImpl->fileWatcher) return;
    const QStringList existing = myImpl->fileWatcher->files();
    if (!existing.isEmpty()) {
        myImpl->fileWatcher->removePaths(existing);
    }
    QStringList toWatch;
    if (!myImpl->stagePath.isEmpty() && QFileInfo::exists(myImpl->stagePath)) {
        toWatch << myImpl->stagePath;
        const QString parentScene = QDir(QFileInfo(myImpl->stagePath).dir().absolutePath()).absoluteFilePath("../scene.usda");
        if (QFileInfo::exists(parentScene)) {
            toWatch << QFileInfo(parentScene).canonicalFilePath();
        }
    }
    if (!myImpl->userEditsPath.isEmpty() && QFileInfo::exists(myImpl->userEditsPath)) {
        toWatch << myImpl->userEditsPath;
    }
    const QString cameraFile = QFileInfo(myImpl->stagePath).dir().filePath("camera_rig.json");
    if (QFileInfo::exists(cameraFile)) toWatch << cameraFile;
    if (!toWatch.isEmpty()) {
        myImpl->fileWatcher->addPaths(toWatch);
    }
}

QString UsdViewportWidget::stagePath() const { return myImpl->stagePath; }
QString UsdViewportWidget::userEditsLayerPath() const { return myImpl->userEditsPath; }
bool UsdViewportWidget::hasStage() const { return static_cast<bool>(myImpl->stage); }
bool UsdViewportWidget::isSceneStage() const { return myImpl->isSceneStage; }

int UsdViewportWidget::snapshotSceneForExistingShots(QString* snapshotFileName) {
    if (myImpl->stagePath.isEmpty()) return 0;
    const QFileInfo sceneInfo(myImpl->stagePath);
    const QDir sceneDir = sceneInfo.dir();

    const QStringList shotDirs = sceneDir.entryList(QStringList() << QStringLiteral("shot_*"), QDir::Dirs, QDir::Name);
    QStringList shotsToRetarget;

    for (const QString& sDirName : shotDirs) {
        const QString shotUsdaPath = sceneDir.filePath(QStringLiteral("%1/shot.usda").arg(sDirName));
        if (!QFileInfo::exists(shotUsdaPath)) continue;

        QFile file(shotUsdaPath);
        if (file.open(QIODevice::ReadOnly | QIODevice::Text)) {
            const QString content = QString::fromUtf8(file.readAll());
            if (content.contains(QStringLiteral("@../scene.usda@")) || content.contains(QStringLiteral("\"../scene.usda\""))) {
                shotsToRetarget.append(shotUsdaPath);
            }
        }
    }

    if (shotsToRetarget.isEmpty()) {
        return 0;
    }

    int nextVersion = 1;
    while (sceneDir.exists(QStringLiteral("scene_v%1.usda").arg(nextVersion))) {
        nextVersion++;
    }

    const QString snapName = QStringLiteral("scene_v%1.usda").arg(nextVersion);
    const QString snapPath = sceneDir.filePath(snapName);

    if (!QFile::copy(sceneInfo.absoluteFilePath(), snapPath)) {
        qWarning() << "Failed to copy scene.usda to" << snapPath;
        return 0;
    }

    const QString newSublayer = QStringLiteral("@../%1@").arg(snapName);
    for (const QString& shotPath : shotsToRetarget) {
        QFile file(shotPath);
        if (file.open(QIODevice::ReadOnly | QIODevice::Text)) {
            QString content = QString::fromUtf8(file.readAll());
            file.close();

            content.replace(QStringLiteral("@../scene.usda@"), newSublayer);
            content.replace(QStringLiteral("\"../scene.usda\""), QStringLiteral("\"../%1\"").arg(snapName));

            QSaveFile out(shotPath);
            if (out.open(QIODevice::WriteOnly | QIODevice::Text)) {
                out.write(content.toUtf8());
                out.commit();
            }
        }
    }

    if (snapshotFileName) {
        *snapshotFileName = snapName;
    }
    return nextVersion;
}

bool UsdViewportWidget::saveSceneEdits(QString* errorMsg) {
    if (!myImpl->stage || !myImpl->userEditsLayer) {
        if (errorMsg) *errorMsg = QStringLiteral("No active stage or user edits layer");
        return false;
    }
    if (!myImpl->isSceneStage) {
        if (errorMsg) *errorMsg = QStringLiteral("Current stage is not the main scene");
        return false;
    }

    // Save pending changes to user_edits.usda first
    myImpl->userEditsLayer->Save();

    const QFileInfo sceneInfo(myImpl->stagePath);
    const QDir sceneDir = sceneInfo.dir();

    // Prefer tools/scene_manager.py for complete pipeline (snapshot, merge, reset, world_state, scene.json)
    const QString projectRoot = QDir::currentPath();
    const QString sceneManagerScript = QDir(projectRoot).filePath(QStringLiteral("tools/scene_manager.py"));
    bool pythonSuccess = false;

    if (QFileInfo::exists(sceneManagerScript)) {
        QProcess proc;
        QString pythonBin = QDir(projectRoot).filePath(QStringLiteral(".venv/bin/python"));
        if (!QFileInfo::exists(pythonBin)) {
            pythonBin = QStringLiteral("python3");
        }
        proc.start(pythonBin, QStringList() << sceneManagerScript << QStringLiteral("save") << sceneDir.absolutePath());
        if (proc.waitForFinished(15000) && proc.exitCode() == 0) {
            pythonSuccess = true;
        } else {
            qWarning() << "scene_manager.py failed or timed out:" << proc.readAllStandardError();
        }
    }

    if (!pythonSuccess) {
        // Native fallback
        QString snapFile;
        snapshotSceneForExistingShots(&snapFile);

        SdfLayerHandle rootLayer = myImpl->stage->GetRootLayer();
        if (!rootLayer) {
            if (errorMsg) *errorMsg = QStringLiteral("Could not access stage root layer");
            return false;
        }

        SdfPrimSpecHandle worldSpec = myImpl->userEditsLayer->GetPrimAtPath(SdfPath("/World"));
        if (worldSpec) {
            auto mergePrim = [&](auto& self, const SdfPrimSpecHandle& srcPrim) -> void {
                const SdfPath p = srcPrim->GetPath();
                SdfPrimSpecHandle dstPrim = rootLayer->GetPrimAtPath(p);
                if (!dstPrim) {
                    const SdfPath parent = p.GetParentPath();
                    if (!rootLayer->GetPrimAtPath(parent)) {
                        SdfCreatePrimInLayer(rootLayer, parent);
                    }
                    SdfCreatePrimInLayer(rootLayer, p);
                    SdfCopySpec(myImpl->userEditsLayer, p, rootLayer, p);
                    return;
                }
                for (const SdfPropertySpecHandle& prop : srcPrim->GetProperties()) {
                    SdfCopySpec(myImpl->userEditsLayer, prop->GetPath(), rootLayer, prop->GetPath());
                }
                if (srcPrim->HasInfo(SdfFieldKeys->CustomData)) {
                    dstPrim->SetInfo(SdfFieldKeys->CustomData, srcPrim->GetInfo(SdfFieldKeys->CustomData));
                }
                if (srcPrim->HasInfo(SdfFieldKeys->Active)) {
                    dstPrim->SetInfo(SdfFieldKeys->Active, srcPrim->GetInfo(SdfFieldKeys->Active));
                }
                for (const SdfPrimSpecHandle& child : srcPrim->GetNameChildren()) {
                    self(self, child);
                }
            };
            for (const SdfPrimSpecHandle& child : worldSpec->GetNameChildren()) {
                mergePrim(mergePrim, child);
            }
        }

        rootLayer->Save();

        const std::string emptyTemplate =
            "#usda 1.0\n(\n    doc = \"PainterSpace manual user edits layer\"\n)\n\nover \"World\"\n{\n}\n";
        myImpl->userEditsLayer->ImportFromString(emptyTemplate);
        myImpl->userEditsLayer->Save();
    }

    // Refresh stage in viewport
    myImpl->suppressWatcherReload = true;
    myImpl->userEditsLayer->Reload();
    myImpl->stage->Reload();
    myImpl->undoStack.clear();
    myImpl->redoStack.clear();
    std::string exported;
    myImpl->userEditsLayer->ExportToString(&exported);
    myImpl->undoStack.append(QString::fromStdString(exported));
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(0);
    return true;
}

void UsdViewportWidget::setPlaying(bool playing) {
    if (myImpl->playing == playing) return;
    myImpl->playing = playing;
    if (playing) {
        myImpl->playTimer->start(std::max(1, static_cast<int>(std::round(1000.0 / myImpl->fps))));
    } else {
        myImpl->playTimer->stop();
    }
    myImpl->hydraDirty = true;
    update();
    updateOverlayUi();
}

bool UsdViewportWidget::isPlaying() const { return myImpl->playing; }
double UsdViewportWidget::currentFrame() const { return myImpl->currentFrame; }

void UsdViewportWidget::setCurrentFrame(double frame) {
    const double clamped = std::clamp(frame, myImpl->startFrame, myImpl->endFrame);
    if (std::abs(myImpl->currentFrame - clamped) < 1e-4) return;
    myImpl->currentFrame = clamped;
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit playbackFrameChanged(myImpl->currentFrame);
}

double UsdViewportWidget::startFrame() const { return myImpl->startFrame; }
double UsdViewportWidget::endFrame() const { return myImpl->endFrame; }
double UsdViewportWidget::fps() const { return myImpl->fps; }

void UsdViewportWidget::setViewMode(ViewMode mode) {
    const bool useShotCam = (mode == ViewMode::CameraViewfinder);
    if (myImpl->viewMode == mode && myImpl->useShotCam == useShotCam) return;
    myImpl->viewMode = mode;
    myImpl->useShotCam = useShotCam;
    if (mode == ViewMode::World && myImpl->cameraTools->tool() == CameraToolType::Aim) {
        myImpl->cameraTools->setTool(CameraToolType::None);
    }
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit viewModeChanged(myImpl->viewMode);
}

ViewMode UsdViewportWidget::viewMode() const {
    return myImpl->viewMode;
}

void UsdViewportWidget::toggleViewMode() {
    setViewMode(myImpl->viewMode == ViewMode::World ? ViewMode::CameraViewfinder : ViewMode::World);
}

void UsdViewportWidget::setUseShotCamera(bool useShotCam) {
    setViewMode(useShotCam ? ViewMode::CameraViewfinder : ViewMode::World);
}

bool UsdViewportWidget::useShotCamera() const { return myImpl->useShotCam; }

CameraToolController* UsdViewportWidget::cameraTools() const {
    return myImpl->cameraTools.get();
}

void UsdViewportWidget::setShotSize(CameraShotSize size) {
    applyShotSizePreset(size, myImpl->cameraTools->rigData().framingMoveCamera);
    emit shotSizeChanged(size);
}

CameraShotSize UsdViewportWidget::shotSize() const {
    return myImpl->cameraTools->rigData().shotSize;
}

void UsdViewportWidget::applyShotSizePreset(CameraShotSize size, bool moveCamera) {
    pushUndoSnapshot();
    auto& rig = myImpl->cameraTools->rigData();
    rig.shotSize = size;
    rig.framingMoveCamera = moveCamera;

    QString subject = !rig.aimTargets.isEmpty() ? rig.aimTargets.first() : myImpl->selectedPrimPath;
    if (subject.isEmpty()) subject = rig.shotSubject;

    double subjectHeight = 1.8;
    QVector3D subjectPos(0.0f, 1.0f, 0.0f);
    if (!subject.isEmpty() && myImpl->stage) {
        UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(subject.toStdString()));
        if (prim) {
            GfBBox3d bbox = myImpl->cachedWorldBound(prim);
            GfRange3d range = bbox.ComputeAlignedRange();
            if (!range.IsEmpty()) {
                subjectHeight = std::max(0.2, range.GetSize()[1]);
                GfVec3d mid = range.GetMidpoint();
                subjectPos = QVector3D(float(mid[0]), float(mid[1]), float(mid[2]));
            }
        }
    }

    CameraRigEvaluator eval(myImpl->fps);
    double horizAp = 36.0, vertAp = 20.25;
    CameraRigEvaluator::sensorDimensions(rig.sensorPreset, horizAp, vertAp);

    if (moveCamera) {
        const double dist = eval.computeShotSizeDistance(size, subjectHeight, rig.focalStart, vertAp);
        QVector3D camPos = (rig.points.isEmpty()) ? QVector3D(subjectPos.x(), subjectPos.y() + 0.3f, subjectPos.z() + static_cast<float>(dist)) : rig.points.first().position;
        QVector3D dir = (camPos - subjectPos).normalized();
        if (dir.lengthSquared() < 0.1f) dir = QVector3D(0.0f, 0.2f, 1.0f).normalized();
        QVector3D newPos = subjectPos + dir * static_cast<float>(dist);

        if (rig.points.isEmpty()) {
            rig.points.append({newPos});
        } else {
            rig.points[0].position = newPos;
        }
    } else {
        QVector3D camPos = (rig.points.isEmpty()) ? QVector3D(0.0f, 1.6f, 5.0f) : rig.points.first().position;
        const double curDist = (camPos - subjectPos).length();
        const double newFocal = eval.computeShotSizeFocal(size, subjectHeight, curDist, vertAp);
        rig.focalStart = newFocal;
        rig.focalEnd = newFocal;
        if (myImpl->zoomSlider) myImpl->zoomSlider->setValue(static_cast<int>(std::round(newFocal)));
    }
    bakeCurrentCameraRig();
}

void UsdViewportWidget::setCameraFocalLength(double focalMm) {
    pushUndoSnapshot();
    const double clamped = std::clamp(focalMm, 12.0, 200.0);
    auto& rig = myImpl->cameraTools->rigData();
    rig.focalStart = clamped;
    rig.focalEnd = clamped;
    if (myImpl->zoomSlider) {
        QSignalBlocker b(myImpl->zoomSlider);
        myImpl->zoomSlider->setValue(static_cast<int>(std::round(clamped)));
    }
    if (myImpl->zoomLabel) {
        myImpl->zoomLabel->setText(QString("%1 mm").arg(static_cast<int>(std::round(clamped))));
    }
    bakeCurrentCameraRig();
}

double UsdViewportWidget::cameraFocalLength() const {
    return myImpl->cameraTools->rigData().focalStart;
}

void UsdViewportWidget::setCameraAspectRatio(const QString& aspect) {
    pushUndoSnapshot();
    myImpl->cameraTools->rigData().aspectRatio = aspect;
    bakeCurrentCameraRig();
}

QString UsdViewportWidget::cameraAspectRatio() const {
    return myImpl->cameraTools->rigData().aspectRatio;
}

void UsdViewportWidget::bakeCurrentCameraRig() {
    if (!myImpl->stage || !myImpl->userEditsLayer) return;
    myImpl->suppressWatcherReload = true;
    myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    const QDir shotDir = QFileInfo(myImpl->stagePath).dir();
    auto doc = myImpl->cameraDocument;
    if (doc.isEmpty()) doc = QJsonObject{{"version", 1}, {"active_camera", "cam_1"}, {"cameras", QJsonObject()}};
    const QString cid = doc["active_camera"].toString("cam_1");
    auto cameras = doc["cameras"].toObject();
    CameraRigData symbolic = myImpl->cameraTools->rigData();
    const auto prims = primList();
    for (auto& target : symbolic.aimTargets) {
        for (const auto& prim : prims) if (prim.path == target && !prim.actorId.isEmpty()) { target = prim.actorId; break; }
    }
    cameras[cid] = cameraRigToJson(symbolic, cameras[cid].toObject());
    doc["cameras"] = cameras; myImpl->cameraDocument = doc;
    const QByteArray bytes = QJsonDocument(doc).toJson(QJsonDocument::Indented);
    QSaveFile jsonFile(shotDir.filePath("camera_rig.json"));
    if (jsonFile.open(QIODevice::WriteOnly)) { jsonFile.write(bytes); jsonFile.commit(); }
    saveCameraRigToStage(myImpl->stage, myImpl->cameraTools->rigData(), myImpl->startFrame, myImpl->endFrame, myImpl->fps);
    const QString cameraFile = shotDir.filePath("cam_" + cid + ".usda");
    auto cameraLayer = SdfLayer::FindOrOpen(cameraFile.toStdString());
    if (!cameraLayer) cameraLayer = SdfLayer::CreateNew(cameraFile.toStdString());
    if (cameraLayer) {
        const auto paths = myImpl->userEditsLayer->GetSubLayerPaths();
        if (std::find(paths.begin(), paths.end(), cameraFile.toStdString()) == paths.end())
            myImpl->userEditsLayer->GetSubLayerPaths().push_back(cameraFile.toStdString());
        cameraLayer->Clear();
        myImpl->stage->SetEditTarget(UsdEditTarget(cameraLayer));
        const QString cameraPath = "/World/Cameras/" + cid;
        bakeCameraRigToShotCam(myImpl->stage, myImpl->cameraTools->rigData(), myImpl->startFrame, myImpl->endFrame, myImpl->fps, {}, cameraPath);
        cameraLayer->Save(); myImpl->shotCamPath = cameraPath;
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }
    myImpl->userEditsLayer->Save();
    myImpl->suppressWatcherReload = false;
    myImpl->hydraDirty = true;
    update();
    syncWatchedFiles();
    myImpl->cameraPreviewTimer->start();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::resetCameraEdits() {
    if (!myImpl->stage || !myImpl->userEditsLayer) return;
    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    resetCameraRigOnStage(myImpl->stage);
    QFile::remove(QFileInfo(myImpl->stagePath).dir().filePath("camera_rig.json"));
    myImpl->cameraDocument = {};
    auto metadata = myImpl->userEditsLayer->GetCustomLayerData();
    metadata.erase("director_camera_document");
    myImpl->userEditsLayer->SetCustomLayerData(metadata);
    myImpl->shotCamPath = "/World/ShotCam";
    myImpl->userEditsLayer->Save();
    myImpl->suppressWatcherReload = false;
    myImpl->cameraTools->clearPath();
    myImpl->hydraDirty = true;
    update();
    emit userEditsChanged(userEditedPrimCount());
    emit statusMessageChanged(QStringLiteral("Camera overrides reset to baseline"));
}

void UsdViewportWidget::convertLlmCameraToManualRig() {
    if (!myImpl->stage) return;
    pushUndoSnapshot();
    CameraRigData rig;
    if (convertShotCamToRig(myImpl->stage, myImpl->startFrame, myImpl->endFrame, myImpl->fps, rig)) {
        myImpl->cameraTools->setRigData(rig);
        bakeCurrentCameraRig();
        emit statusMessageChanged(QStringLiteral("Converted LLM camera to editable manual rig"));
    }
}

QRectF UsdViewportWidget::viewfinderCropRect(double targetAspect) const {
    const double w = std::max(64, width());
    const double h = std::max(64, height());
    const double currentAspect = w / h;
    const double target = targetAspect > 0.1 ? targetAspect : (16.0 / 9.0);

    if (currentAspect > target) {
        const double cropW = h * target;
        const double x = (w - cropW) * 0.5;
        return QRectF(x, 0, cropW, h);
    } else {
        const double cropH = w / target;
        const double y = (h - cropH) * 0.5;
        return QRectF(0, y, w, cropH);
    }
}
QString UsdViewportWidget::activeCameraPath() const {
    return myImpl->useShotCam ? myImpl->shotCamPath : QString();
}

void UsdViewportWidget::resetFreeCamera() {
    setViewMode(ViewMode::World);
    myImpl->yaw = 0.45;
    myImpl->pitch = 0.32;
    myImpl->distance = 12.0;
    myImpl->target = QVector3D(0.0f, 1.6f, -2.0f);
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
}

void UsdViewportWidget::frameSelectedPrim() {
    if (!myImpl->stage) return;
    if (myImpl->selectedAimTarget) {
        myImpl->target = resolvedAimTargetPosition();
        myImpl->distance = 8.0;
        setViewMode(ViewMode::World);
        myImpl->hydraDirty = true;
        updateOverlayUi();
        update();
        return;
    }
    if (myImpl->selectedPrimPath.isEmpty()) return;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim) return;
    GfBBox3d bbox = myImpl->cachedWorldBound(prim);
    GfRange3d range = bbox.ComputeAlignedRange();
    if (!range.IsEmpty()) {
        GfVec3d c = range.GetMidpoint();
        GfVec3d sz = range.GetSize();
        myImpl->target = QVector3D(float(c[0]), float(c[1]), float(c[2]));
        myImpl->distance = std::clamp(sz.GetLength() * 1.4, 2.5, 80.0);
        setViewMode(ViewMode::World);
        myImpl->hydraDirty = true;
        updateOverlayUi();
        update();
    }
}

QVector3D UsdViewportWidget::cameraEvaluatedPosition() const {
    if (myImpl->cameraTools) {
        CameraRigEvaluator evaluator(myImpl->fps);
        return evaluator.evaluate(myImpl->cameraTools->rigData(), myImpl->currentFrame / myImpl->fps).position;
    }
    return QVector3D(0.0f, 1.6f, 5.0f);
}

QVector3D UsdViewportWidget::cameraEvaluatedRotation() const {
    if (myImpl->cameraTools) {
        CameraRigEvaluator evaluator(myImpl->fps);
        return evaluator.evaluate(myImpl->cameraTools->rigData(), myImpl->currentFrame / myImpl->fps).rotationDeg;
    }
    return QVector3D(0.0f, 0.0f, 0.0f);
}

QVector3D UsdViewportWidget::resolvedAimTargetPosition() const {
    if (!myImpl->cameraTools) return QVector3D(0.0f, 1.2f, 0.0f);
    const CameraRigData& rig = myImpl->cameraTools->rigData();
    if (!rig.aimTargets.isEmpty() || !rig.shotSubject.isEmpty()) {
        const QString tgt = !rig.aimTargets.isEmpty() ? rig.aimTargets.first() : rig.shotSubject;
        if (myImpl->stage) {
            UsdPrim tgtPrim = myImpl->stage->GetPrimAtPath(SdfPath(tgt.toStdString()));
            if (tgtPrim) {
                GfBBox3d bbox = myImpl->cachedWorldBound(tgtPrim);
                GfRange3d range = bbox.ComputeAlignedRange();
                if (!range.IsEmpty()) {
                    GfVec3d m = range.GetMidpoint();
                    return QVector3D(float(m[0]), float(m[1]), float(m[2])) + rig.aimOffset;
                }
            }
        }
    }
    return rig.lookAtPoint + rig.aimOffset;
}

void UsdViewportWidget::setAimTargetPosition(const QVector3D& pos) {
    if (!myImpl->cameraTools) return;
    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingAimTarget);
    if (!interactiveDrag) {
        pushUndoSnapshot();
    }
    CameraRigData& rig = myImpl->cameraTools->rigData();
    rig.aimTargets.clear();
    rig.shotSubject.clear();
    rig.lookAtPoint = pos - rig.aimOffset;
    rig.hasCustomLookAt = true;
    rig.aimMode = CameraAimMode::LookAt;

    if (interactiveDrag) {
        myImpl->pendingDragSave = true;
        if (myImpl->stage && myImpl->userEditsLayer) {
            myImpl->suppressWatcherReload = true;
            myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
            bakeCameraRigToShotCam(myImpl->stage, rig, myImpl->startFrame, myImpl->endFrame, myImpl->fps);
            myImpl->suppressWatcherReload = false;
        }
        myImpl->hydraDirty = true;
        update();
        emit stageChanged();
        return;
    }

    bakeCurrentCameraRig();
    if (myImpl->stage && myImpl->userEditsLayer) {
        myImpl->suppressWatcherReload = true;
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        saveCameraRigToStage(myImpl->stage, rig, myImpl->startFrame, myImpl->endFrame, myImpl->fps);
        myImpl->userEditsLayer->Save();
        myImpl->suppressWatcherReload = false;
    }
    myImpl->hydraDirty = true;
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

bool UsdViewportWidget::isAimTargetSelected() const {
    return myImpl->selectedAimTarget;
}

void UsdViewportWidget::setAimTargetSelected(bool selected) {
    myImpl->selectedAimTarget = selected;
    if (selected) {
        myImpl->selectedPrimPath.clear();
        if (myImpl->cameraTools) {
            myImpl->cameraTools->setSelectedPointIndex(-1);
        }
    }
    updateOverlayUi();
    update();
}

void UsdViewportWidget::resetAimTargetToCenter() {
    QVector3D center(0.0f, 1.2f, 0.0f);
    if (myImpl->stage) {
        UsdPrim worldPrim = myImpl->stage->GetPrimAtPath(SdfPath("/World"));
        if (worldPrim) {
            GfBBox3d bbox = myImpl->cachedWorldBound(worldPrim);
            GfRange3d range = bbox.ComputeAlignedRange();
            if (!range.IsEmpty()) {
                GfVec3d m = range.GetMidpoint();
                center = QVector3D(float(m[0]), std::max(0.5f, float(m[1])), float(m[2]));
            }
        }
    }
    pushUndoSnapshot();
    setAimTargetPosition(center);
}

void UsdViewportWidget::extendCameraPath() {
    if (!myImpl->cameraTools) return;
    pushUndoSnapshot();
    CameraRigData& rig = myImpl->cameraTools->rigData();
    QVector3D newPt;
    if (rig.points.isEmpty()) {
        newPt = cameraEvaluatedPosition();
    } else if (rig.points.size() == 1) {
        CameraSample sample = CameraRigEvaluator(myImpl->fps).evaluate(rig, myImpl->currentFrame / myImpl->fps);
        QMatrix4x4 m;
        m.rotate(sample.rotationDeg.y(), 0.0f, 1.0f, 0.0f);
        m.rotate(sample.rotationDeg.x(), 1.0f, 0.0f, 0.0f);
        QVector3D fwd = m.mapVector(QVector3D(0.0f, 0.0f, -1.0f));
        newPt = rig.points.last().position + fwd * 2.0f;
    } else {
        const int n = rig.points.size();
        QVector3D dir = rig.points[n - 1].position - rig.points[n - 2].position;
        if (dir.length() < 0.1f) {
            dir = QVector3D(0.0f, 0.0f, -2.0f);
        } else {
            dir = dir.normalized() * std::clamp(dir.length(), 1.2f, 3.5f);
        }
        newPt = rig.points.last().position + dir;
    }
    CameraRigPoint rp;
    rp.position = newPt;
    rig.points.append(rp);
    const int newIdx = rig.points.size() - 1;
    myImpl->cameraTools->setSelectedPointIndex(newIdx);
    selectPrim(myImpl->shotCamPath.isEmpty() ? QStringLiteral("/World/ShotCam") : myImpl->shotCamPath);
    bakeCurrentCameraRig();
    if (myImpl->stage && myImpl->userEditsLayer) {
        myImpl->suppressWatcherReload = true;
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        saveCameraRigToStage(myImpl->stage, rig, myImpl->startFrame, myImpl->endFrame, myImpl->fps);
        myImpl->userEditsLayer->Save();
        myImpl->suppressWatcherReload = false;
    }
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    emit statusMessageChanged(QString("Extended camera path to Joint %1").arg(newIdx + 1));
}

void UsdViewportWidget::deleteLastCameraPathJoint() {
    if (!myImpl->cameraTools) return;
    CameraRigData& rig = myImpl->cameraTools->rigData();
    if (rig.points.isEmpty()) return;

    pushUndoSnapshot();
    rig.points.removeLast();
    const int newSel = rig.points.isEmpty() ? -1 : std::min(myImpl->cameraTools->selectedPointIndex(), static_cast<int>(rig.points.size()) - 1);
    myImpl->cameraTools->setSelectedPointIndex(newSel);

    bakeCurrentCameraRig();
    if (myImpl->stage && myImpl->userEditsLayer) {
        myImpl->suppressWatcherReload = true;
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        saveCameraRigToStage(myImpl->stage, rig, myImpl->startFrame, myImpl->endFrame, myImpl->fps);
        myImpl->userEditsLayer->Save();
        myImpl->suppressWatcherReload = false;
    }
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    emit statusMessageChanged(QString("Deleted last joint. Remaining: %1").arg(rig.points.size()));
}

void UsdViewportWidget::deleteSelectedCameraPathJoint() {
    if (!myImpl->cameraTools) return;
    CameraRigData& rig = myImpl->cameraTools->rigData();
    if (rig.points.isEmpty()) return;

    const int selIdx = myImpl->cameraTools->selectedPointIndex();
    if (selIdx < 0 || selIdx >= rig.points.size()) {
        deleteLastCameraPathJoint();
        return;
    }

    pushUndoSnapshot();
    rig.points.removeAt(selIdx);
    const int newSel = rig.points.isEmpty() ? -1 : std::clamp(selIdx, 0, static_cast<int>(rig.points.size()) - 1);
    myImpl->cameraTools->setSelectedPointIndex(newSel);

    bakeCurrentCameraRig();
    if (myImpl->stage && myImpl->userEditsLayer) {
        myImpl->suppressWatcherReload = true;
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        saveCameraRigToStage(myImpl->stage, rig, myImpl->startFrame, myImpl->endFrame, myImpl->fps);
        myImpl->userEditsLayer->Save();
        myImpl->suppressWatcherReload = false;
    }
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    emit statusMessageChanged(QString("Deleted Joint %1. Remaining: %2").arg(selIdx + 1).arg(rig.points.size()));
}

void UsdViewportWidget::clearCameraPath() {
    if (!myImpl->cameraTools) return;
    CameraRigData& rig = myImpl->cameraTools->rigData();
    if (rig.points.isEmpty()) return;

    pushUndoSnapshot();
    const QVector3D curPos = cameraEvaluatedPosition();
    rig.points.clear();
    rig.pathDrawn = false;
    CameraRigPoint rp;
    rp.position = curPos;
    rig.points.append(rp);
    myImpl->cameraTools->setSelectedPointIndex(-1);

    bakeCurrentCameraRig();
    if (myImpl->stage && myImpl->userEditsLayer) {
        myImpl->suppressWatcherReload = true;
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        saveCameraRigToStage(myImpl->stage, rig, myImpl->startFrame, myImpl->endFrame, myImpl->fps);
        myImpl->userEditsLayer->Save();
        myImpl->suppressWatcherReload = false;
    }
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    emit statusMessageChanged(QStringLiteral("Cleared entire camera path"));
}

int UsdViewportWidget::cameraPathJointCount() const {
    if (!myImpl->cameraTools) return 0;
    const auto& rig = myImpl->cameraTools->rigData();
    return rig.pathDrawn ? rig.points.size() : 0;
}

int UsdViewportWidget::selectedCameraPathJointIndex() const {
    if (!myImpl->cameraTools) return -1;
    return myImpl->cameraTools->selectedPointIndex();
}

void UsdViewportWidget::selectCameraPathJoint(int index) {
    if (!myImpl->cameraTools) return;
    myImpl->cameraTools->setSelectedPointIndex(index);
    if (index >= 0) {
        myImpl->selectedAimTarget = false;
        selectPrim(myImpl->shotCamPath.isEmpty() ? QStringLiteral("/World/ShotCam") : myImpl->shotCamPath);
    }
    updateOverlayUi();
    update();
}

bool UsdViewportWidget::isDrawPathActive() const {
    return myImpl->drawPathActive;
}

void UsdViewportWidget::startDrawingPath(const QString& primPath) {
    QString target = primPath.isEmpty() ? myImpl->selectedPrimPath : primPath;
    if (target.isEmpty()) {
        target = myImpl->shotCamPath.isEmpty() ? QStringLiteral("/World/ShotCam") : myImpl->shotCamPath;
    }
    myImpl->drawingPrimPath = target;
    myImpl->drawPathActive = true;
    myImpl->isDrawingPrimStroke = false;
    myImpl->activePrimStroke.clear();

    // Ensure we are in shot designing stage at starting frame
    setCurrentFrame(myImpl->startFrame);

    if (myImpl->viewMode == ViewMode::CameraViewfinder) {
        toggleViewMode();
    }

    const bool isCam = (target == myImpl->shotCamPath || target == QStringLiteral("/World/ShotCam") ||
                        target.startsWith(QStringLiteral("/World/CameraRig")));
    if (isCam) {
        if (myImpl->cameraTools) {
            myImpl->cameraTools->setTool(CameraToolType::DrawPath);
        }
    } else {
        if (myImpl->cameraTools) {
            myImpl->cameraTools->setTool(CameraToolType::None);
        }
    }

    emit drawPathModeChanged(true, target);
    emit statusMessageChanged(QString("Draw Path: Click & drag in 3D viewport to sketch path for %1").arg(target));
    update();
}

void UsdViewportWidget::stopDrawingPath() {
    const QString prev = myImpl->drawingPrimPath;
    myImpl->drawPathActive = false;
    myImpl->drawingPrimPath.clear();
    myImpl->isDrawingPrimStroke = false;
    myImpl->activePrimStroke.clear();
    if (myImpl->cameraTools && myImpl->cameraTools->tool() == CameraToolType::DrawPath) {
        myImpl->cameraTools->setTool(CameraToolType::None);
    }
    emit drawPathModeChanged(false, prev);
    emit statusMessageChanged(QStringLiteral("Draw Path mode deactivated"));
    update();
}

QString UsdViewportWidget::drawingPrimPath() const {
    return myImpl->drawingPrimPath;
}

bool UsdViewportWidget::hasPrimPath(const QString& primPath) const {
    if (primPath == myImpl->shotCamPath || primPath == QStringLiteral("/World/ShotCam") ||
        primPath.startsWith(QStringLiteral("/World/CameraRig"))) {
        return myImpl->cameraTools && myImpl->cameraTools->rigData().pathDrawn &&
               myImpl->cameraTools->rigData().points.size() >= 2;
    }
    return myImpl->explicitlyDrawnPrimPaths.contains(primPath) &&
           myImpl->primPaths.value(primPath).size() >= 2;
}

QVector<QVector3D> UsdViewportWidget::primPath(const QString& primPath) const {
    if (primPath == myImpl->shotCamPath || primPath == QStringLiteral("/World/ShotCam") ||
        primPath.startsWith(QStringLiteral("/World/CameraRig"))) {
        QVector<QVector3D> pts;
        if (myImpl->cameraTools && myImpl->cameraTools->rigData().pathDrawn) {
            for (const auto& rp : myImpl->cameraTools->rigData().points) {
                pts.append(rp.position);
            }
        }
        return pts;
    }
    return myImpl->primPaths.value(primPath);
}

void UsdViewportWidget::setPrimPath(const QString& primPath, const QVector<QVector3D>& points) {
    if (primPath.isEmpty()) return;
    pushUndoSnapshot();
    const bool isCam = (primPath == myImpl->shotCamPath || primPath == QStringLiteral("/World/ShotCam") ||
                        primPath.startsWith(QStringLiteral("/World/CameraRig")));
    if (isCam) {
        if (myImpl->cameraTools) {
            CameraRigData rig = myImpl->cameraTools->rigData();
            QVector3D camPos;
            if (!rig.points.isEmpty()) {
                camPos = rig.points.first().position;
            } else {
                camPos = cameraEvaluatedPosition();
            }
            rig.points.clear();
            CameraRigPoint startPt;
            startPt.position = camPos;
            startPt.focalLength = rig.focalStart;
            rig.points.append(startPt);

            const int startIdx = (!points.isEmpty() && (points.first() - camPos).length() < 0.35f) ? 1 : 0;
            for (int i = startIdx; i < points.size(); ++i) {
                CameraRigPoint rp;
                rp.position = points[i];
                rp.focalLength = rig.focalEnd;
                rig.points.append(rp);
            }
            if (rig.pathEndS <= rig.pathStartS) {
                rig.pathEndS = rig.pathStartS + 5.0;
            }
            myImpl->cameraTools->setRigData(rig);
            myImpl->cameraTools->setSelectedPointIndex(0);
            setCurrentFrame(myImpl->startFrame);
            bakeCurrentCameraRig();
        }
    } else {
        myImpl->primPaths[primPath] = points;
    }
    persistPrimPaths();
    emit primPathChanged(primPath);
    update();
}

void UsdViewportWidget::clearPrimPath(const QString& primPath) {
    if (primPath.isEmpty()) return;
    pushUndoSnapshot();
    const bool isCam = (primPath == myImpl->shotCamPath || primPath == QStringLiteral("/World/ShotCam") ||
                        primPath.startsWith(QStringLiteral("/World/CameraRig")));
    if (isCam) {
        clearCameraPath();
    } else {
        myImpl->primPaths.remove(primPath);
        myImpl->explicitlyDrawnPrimPaths.remove(primPath);
        if (myImpl->lastDrawnPrimPath == primPath) myImpl->lastDrawnPrimPath.clear();
    }
    persistPrimPaths();
    emit primPathChanged(primPath);
    update();
    emit statusMessageChanged(QString("Cleared movement path for %1").arg(primPath));
}

void UsdViewportWidget::clearAllPrimPaths() {
    pushUndoSnapshot();
    myImpl->primPaths.clear();
    myImpl->explicitlyDrawnPrimPaths.clear();
    myImpl->lastDrawnPrimPath.clear();
    clearCameraPath();
    persistPrimPaths();
    emit primPathChanged(QString());
    update();
}

int UsdViewportWidget::primPathJointCount(const QString& primPath) const {
    if (primPath == myImpl->shotCamPath || primPath == QStringLiteral("/World/ShotCam") ||
        primPath.startsWith(QStringLiteral("/World/CameraRig"))) {
        return cameraPathJointCount();
    }
    return myImpl->explicitlyDrawnPrimPaths.contains(primPath)
        ? myImpl->primPaths.value(primPath).size() : 0;
}

QColor UsdViewportWidget::primPathColor(const QString& primPath) const {
    if (primPath == myImpl->shotCamPath || primPath == QStringLiteral("/World/ShotCam") ||
        primPath.startsWith(QStringLiteral("/World/CameraRig"))) {
        return QColor("#38bdf8"); // Sky Blue for camera
    }
    if (myImpl->primPathColors.contains(primPath)) {
        return myImpl->primPathColors.value(primPath);
    }
    const auto items = primList();
    for (const auto& item : items) {
        if (item.path == primPath && item.identityColor.isValid()) {
            return item.identityColor;
        }
    }
    static const QVector<QColor> sPalette = {
        QColor("#22c55e"), // Vivid Green
        QColor("#f43f5e"), // Bright Rose
        QColor("#eab308"), // Warm Yellow
        QColor("#a855f7"), // Vibrant Purple
        QColor("#f97316"), // Vibrant Orange
        QColor("#06b6d4"), // Cyan
        QColor("#ec4899"), // Pink
        QColor("#14b8a6"), // Teal
        QColor("#3b82f6"), // Cobalt Blue
        QColor("#84cc16")  // Lime
    };
    const uint h = qHash(primPath);
    return sPalette.at(h % sPalette.size());
}

QHash<QString, QVector<QVector3D>> UsdViewportWidget::allPrimPaths() const {
    return myImpl->primPaths;
}

QStringList UsdViewportWidget::explicitlyDrawnPrimPaths() const {
    return myImpl->explicitlyDrawnPrimPaths.values();
}

void UsdViewportWidget::setExplicitlyDrawnPrimPaths(const QStringList& paths) {
    myImpl->explicitlyDrawnPrimPaths.clear();
    for (const auto& path : paths) {
        if (myImpl->primPaths.value(path).size() >= 2) myImpl->explicitlyDrawnPrimPaths.insert(path);
    }
    update();
}

void UsdViewportWidget::setAllPrimPaths(const QHash<QString, QVector<QVector3D>>& paths, bool recordUndo) {
    if (recordUndo) pushUndoSnapshot();
    QHash<QString, QVector<QVector3D>> drawnPaths;
    if (!recordUndo) {
        for (const auto& path : myImpl->explicitlyDrawnPrimPaths) {
            if (myImpl->primPaths.value(path).size() >= 2)
                drawnPaths.insert(path, myImpl->primPaths.value(path));
        }
    }
    myImpl->primPaths = paths;
    if (recordUndo) {
        myImpl->explicitlyDrawnPrimPaths.clear();
        myImpl->lastDrawnPrimPath.clear();
    } else {
        for (auto it = drawnPaths.cbegin(); it != drawnPaths.cend(); ++it)
            myImpl->primPaths.insert(it.key(), it.value());
    }
    if (recordUndo) persistPrimPaths();
    update();
}

QVector<UsdPrimItem> UsdViewportWidget::primList() const {
    QVector<UsdPrimItem> items;
    if (!myImpl->stage) return items;

    UsdPrim worldPrim = myImpl->stage->GetPrimAtPath(SdfPath("/World"));
    if (!worldPrim) worldPrim = myImpl->stage->GetPseudoRoot();

    QStringList editedPaths;
    if (myImpl->userEditsLayer) {
        collectEditedPrimsInSpec(myImpl->userEditsLayer->GetPseudoRoot(), editedPaths);
    }

    for (const UsdPrim& child : worldPrim.GetChildren()) {
        const std::string nameStr = child.GetName().GetString();
        if (nameStr == "Zones") continue;
        UsdPrimItem item;
        item.path = QString::fromStdString(child.GetPath().GetString());
        item.name = QString::fromStdString(nameStr);
        item.typeName = QString::fromStdString(child.GetTypeName().GetString());

        VtValue assetIdVal = child.GetCustomDataByKey(TfToken("asset_id"));
        if (assetIdVal.IsHolding<std::string>()) {
            item.assetId = QString::fromStdString(assetIdVal.UncheckedGet<std::string>());
        }

        VtValue actorIdVal = child.GetCustomDataByKey(TfToken("actor_id"));
        if (actorIdVal.IsHolding<std::string>()) {
            item.actorId = QString::fromStdString(actorIdVal.UncheckedGet<std::string>());
        }

        VtValue labelVal = child.GetCustomDataByKey(TfToken("label"));
        if (labelVal.IsHolding<std::string>()) {
            item.actorLabel = QString::fromStdString(labelVal.UncheckedGet<std::string>());
        }

        VtValue appVarVal = child.GetCustomDataByKey(TfToken("appearance_variant"));
        if (appVarVal.IsHolding<std::string>()) {
            item.appearanceVariant = QString::fromStdString(appVarVal.UncheckedGet<std::string>());
        }

        VtValue tintVal = child.GetCustomDataByKey(TfToken("tint_rgb"));
        if (tintVal.IsHolding<GfVec3f>()) {
            GfVec3f rgb = tintVal.UncheckedGet<GfVec3f>();
            item.identityColor = QColor::fromRgbF(rgb[0], rgb[1], rgb[2]);
        } else {
            const QString colorKey = !item.actorId.isEmpty() ? item.actorId : item.name;
            const int h = qAbs(qHash(colorKey)) % 360;
            item.identityColor = QColor::fromHsv(h, 175, 230);
        }

        UsdGeomImageable img(child);
        if (img) {
            TfToken vis = img.ComputeVisibility(UsdTimeCode(myImpl->currentFrame));
            item.visible = (vis != UsdGeomTokens->invisible);
        } else {
            item.visible = child.IsActive();
        }

        item.hasUserEdit = editedPaths.contains(item.path);
        item.definedIn = item.hasUserEdit ? QStringLiteral("user_edits")
                                          : (myImpl->isSceneStage ? QStringLiteral("scene") : QStringLiteral("shot"));
        items.append(item);
    }
    return items;
}

QString UsdViewportWidget::selectedPrimPath() const { return myImpl->selectedPrimPath; }

void UsdViewportWidget::selectPrim(const QString& primPath) {
    if (myImpl->selectedPrimPath == primPath) return;
    myImpl->lastDrawnPrimPath.clear();
    myImpl->selectedPrimPath = primPath;
    if (!primPath.isEmpty()) {
        myImpl->selectedAimTarget = false;
    }
    // Selection box & gizmo are drawn as a Qt 2D overlay over cachedFrame; no Hydra re-render needed.
    updateOverlayUi();
    update();
    emit selectionChanged(myImpl->selectedPrimPath);
}

void UsdViewportWidget::setPrimVisible(const QString& primPath, bool visible) {
    if (!myImpl->stage || primPath.isEmpty()) return;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(primPath.toStdString()));
    if (!prim) return;

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }
    UsdGeomImageable img(prim);
    if (img) {
        if (visible) {
            img.MakeVisible();
        } else {
            img.MakeInvisible();
        }
    }
    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

QVector3D UsdViewportWidget::selectedPrimPosition() const {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return QVector3D();

    if (myImpl->selectedPrimPath == myImpl->shotCamPath || myImpl->selectedPrimPath == QStringLiteral("/World/ShotCam")) {
        if (myImpl->cameraTools) {
            const auto& rig = myImpl->cameraTools->rigData();
            const int selPt = myImpl->cameraTools->selectedPointIndex();
            if (selPt >= 0 && selPt < rig.points.size()) {
                return rig.points[selPt].position;
            }
        }
        return cameraEvaluatedPosition();
    }

    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return QVector3D();
    UsdGeomXformable xf(prim);
    GfMatrix4d localXf(1.0);
    bool resets = false;
    xf.GetLocalTransformation(&localXf, &resets, UsdTimeCode(myImpl->currentFrame));
    GfVec3d t = localXf.ExtractTranslation();
    return QVector3D(float(t[0]), float(t[1]), float(t[2]));
}

bool UsdViewportWidget::primWorldPosition(const QString& primPath, QVector3D& position) const {
    if (!myImpl->stage || primPath.isEmpty()) return false;
    const UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(primPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return false;
    const auto translation = UsdGeomXformable(prim)
                                 .ComputeLocalToWorldTransform(UsdTimeCode(myImpl->startFrame))
                                 .ExtractTranslation();
    position = QVector3D(float(translation[0]), float(translation[1]), float(translation[2]));
    return true;
}

void UsdViewportWidget::setSelectedPrimPosition(const QVector3D& position) {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;

    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingObjectGround);
    if (!interactiveDrag) {
        pushUndoSnapshot();
    }

    if (myImpl->selectedPrimPath == myImpl->shotCamPath || myImpl->selectedPrimPath == QStringLiteral("/World/ShotCam")) {
        if (myImpl->cameraTools) {
            CameraRigData& rig = myImpl->cameraTools->rigData();
            const int selPt = myImpl->cameraTools->selectedPointIndex();
            if (selPt >= 0 && selPt < rig.points.size()) {
                rig.points[selPt].position = position;
            } else if (rig.points.size() == 1) {
                rig.points[0].position = position;
            } else if (rig.points.isEmpty()) {
                CameraRigPoint rp;
                rp.position = position;
                rig.points.append(rp);
            } else {
                const QVector3D curPos = cameraEvaluatedPosition();
                const QVector3D delta = position - curPos;
                for (auto& pt : rig.points) {
                    pt.position += delta;
                }
            }
        }

        if (interactiveDrag) {
            myImpl->pendingDragSave = true;
            if (myImpl->stage && myImpl->userEditsLayer && myImpl->cameraTools) {
                myImpl->suppressWatcherReload = true;
                myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
                bakeCameraRigToShotCam(myImpl->stage, myImpl->cameraTools->rigData(), myImpl->startFrame, myImpl->endFrame, myImpl->fps);
                myImpl->suppressWatcherReload = false;
            }
            myImpl->hydraDirty = true;
            update();
            emit selectionChanged(myImpl->selectedPrimPath);
            return;
        }

        bakeCurrentCameraRig();
        updateOverlayUi();
        update();
        emit stageChanged();
        emit userEditsChanged(userEditedPrimCount());
        return;
    }

    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return;

    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    UsdGeomXformable xf(prim);
    bool resets = false;
    std::vector<UsdGeomXformOp> ops = xf.GetOrderedXformOps(&resets);
    UsdGeomXformOp translateOp;
    for (const UsdGeomXformOp& op : ops) {
        if (op.GetOpType() == UsdGeomXformOp::TypeTranslate) {
            translateOp = op;
            break;
        }
    }
    if (!translateOp) {
        translateOp = xf.AddTranslateOp(UsdGeomXformOp::PrecisionDouble);
    }
    const GfVec3d val(position.x(), position.y(), position.z());
    if (translateOp.GetNumTimeSamples() > 0) {
        translateOp.Set(val, UsdTimeCode(myImpl->currentFrame));
    } else {
        translateOp.Set(val);
    }

    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->hydraDirty = true;

    if (interactiveDrag) {
        myImpl->pendingDragSave = true;
        myImpl->suppressWatcherReload = false;
        update();
        emit selectionChanged(myImpl->selectedPrimPath);
        return;
    }

    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

QVector3D UsdViewportWidget::selectedPrimRotation() const {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return QVector3D();
    if (myImpl->selectedPrimPath == myImpl->shotCamPath || myImpl->selectedPrimPath == QStringLiteral("/World/ShotCam")) {
        return cameraEvaluatedRotation();
    }
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return QVector3D();
    UsdGeomXformable xf(prim);
    bool resets = false;
    for (const UsdGeomXformOp& op : xf.GetOrderedXformOps(&resets)) {
        if (op.GetOpType() == UsdGeomXformOp::TypeRotateXYZ) {
            GfVec3f r(0.0f);
            if (op.Get(&r, UsdTimeCode(myImpl->currentFrame))) {
                return QVector3D(r[0], r[1], r[2]);
            }
            GfVec3d rd(0.0);
            if (op.Get(&rd, UsdTimeCode(myImpl->currentFrame))) {
                return QVector3D(float(rd[0]), float(rd[1]), float(rd[2]));
            }
        } else if (op.GetOpType() == UsdGeomXformOp::TypeRotateY) {
            float ry = 0.0f;
            if (op.Get(&ry, UsdTimeCode(myImpl->currentFrame))) {
                return QVector3D(0.0f, ry, 0.0f);
            }
            double ryd = 0.0;
            if (op.Get(&ryd, UsdTimeCode(myImpl->currentFrame))) {
                return QVector3D(0.0f, float(ryd), 0.0f);
            }
        }
    }
    return QVector3D();
}

void UsdViewportWidget::setSelectedPrimRotation(const QVector3D& rotationDeg) {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;

    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingObjectGround);
    if (!interactiveDrag) {
        pushUndoSnapshot();
    }

    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return;

    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    UsdGeomXformable xf(prim);
    bool resets = false;
    std::vector<UsdGeomXformOp> ops = xf.GetOrderedXformOps(&resets);
    UsdGeomXformOp rotXYZOp;
    UsdGeomXformOp rotYOp;
    for (const UsdGeomXformOp& op : ops) {
        if (op.GetOpType() == UsdGeomXformOp::TypeRotateXYZ) rotXYZOp = op;
        if (op.GetOpType() == UsdGeomXformOp::TypeRotateY) rotYOp = op;
    }

    if (rotYOp && std::abs(rotationDeg.x()) < 1e-3f && std::abs(rotationDeg.z()) < 1e-3f) {
        if (rotYOp.GetNumTimeSamples() > 0) {
            rotYOp.Set(rotationDeg.y(), UsdTimeCode(myImpl->currentFrame));
        } else {
            rotYOp.Set(rotationDeg.y());
        }
    } else {
        if (!rotXYZOp) {
            if (ops.empty()) {
                xf.AddTranslateOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(0.0));
            }
            rotXYZOp = xf.AddRotateXYZOp(UsdGeomXformOp::PrecisionFloat);
        }
        const GfVec3f val(rotationDeg.x(), rotationDeg.y(), rotationDeg.z());
        if (rotXYZOp.GetNumTimeSamples() > 0) {
            rotXYZOp.Set(val, UsdTimeCode(myImpl->currentFrame));
        } else {
            rotXYZOp.Set(val);
        }
    }

    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->hydraDirty = true;

    if (interactiveDrag) {
        myImpl->pendingDragSave = true;
        myImpl->suppressWatcherReload = false;
        update();
        emit selectionChanged(myImpl->selectedPrimPath);
        return;
    }

    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

double UsdViewportWidget::selectedPrimScale() const {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return 1.0;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return 1.0;
    UsdGeomXformable xf(prim);
    bool resets = false;
    for (const UsdGeomXformOp& op : xf.GetOrderedXformOps(&resets)) {
        if (op.GetOpType() == UsdGeomXformOp::TypeScale) {
            GfVec3d sd(1.0);
            if (op.Get(&sd, UsdTimeCode(myImpl->currentFrame))) return sd[0];
            GfVec3f sf(1.0f);
            if (op.Get(&sf, UsdTimeCode(myImpl->currentFrame))) return sf[0];
        }
    }
    return 1.0;
}

void UsdViewportWidget::setSelectedPrimScale(double scale) {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;

    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingObjectGround);
    if (!interactiveDrag) {
        pushUndoSnapshot();
    }

    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return;

    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    UsdGeomXformable xf(prim);
    bool resets = false;
    std::vector<UsdGeomXformOp> ops = xf.GetOrderedXformOps(&resets);
    UsdGeomXformOp scaleOp;
    for (const UsdGeomXformOp& op : ops) {
        if (op.GetOpType() == UsdGeomXformOp::TypeScale) {
            scaleOp = op;
            break;
        }
    }
    if (!scaleOp) {
        if (ops.empty()) {
            xf.AddTranslateOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(0.0));
        }
        scaleOp = xf.AddScaleOp(UsdGeomXformOp::PrecisionDouble);
    }
    const double s = std::max(0.01, scale);
    if (scaleOp.GetPrecision() == UsdGeomXformOp::PrecisionFloat) {
        scaleOp.Set(GfVec3f(float(s), float(s), float(s)));
    } else {
        scaleOp.Set(GfVec3d(s, s, s));
    }

    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->hydraDirty = true;

    if (interactiveDrag) {
        myImpl->pendingDragSave = true;
        myImpl->suppressWatcherReload = false;
        update();
        emit selectionChanged(myImpl->selectedPrimPath);
        return;
    }

    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::deleteSelectedPrim() {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim) return;

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }
    UsdGeomImageable img(prim);
    if (img) {
        img.MakeInvisible();
    }
    prim.SetActive(false);
    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->selectedPrimPath.clear();
    myImpl->suppressWatcherReload = false;
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit selectionChanged(QString());
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::duplicateSelectedPrim() {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;
    UsdPrim srcPrim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!srcPrim) return;

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    const std::string baseName = srcPrim.GetName().GetString();
    SdfPath newPath;
    for (int i = 2; i < 100; ++i) {
        SdfPath candidate = SdfPath("/World/" + baseName + "_" + std::to_string(i));
        if (!myImpl->stage->GetPrimAtPath(candidate)) {
            newPath = candidate;
            break;
        }
    }
    if (!newPath.IsEmpty()) {
        UsdGeomXform dstXform = UsdGeomXform::Define(myImpl->stage, newPath);
        QVector3D pos = selectedPrimPosition() + QVector3D(1.5f, 0.0f, 0.0f);
        dstXform.AddTranslateOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(pos.x(), pos.y(), pos.z()));
        VtValue assetIdVal = srcPrim.GetCustomDataByKey(TfToken("asset_id"));
        if (!assetIdVal.IsEmpty()) {
            dstXform.GetPrim().SetCustomDataByKey(TfToken("asset_id"), assetIdVal);
        }
        if (myImpl->userEditsLayer) {
            myImpl->userEditsLayer->Save();
        }
        myImpl->selectedPrimPath = QString::fromStdString(newPath.GetString());
        myImpl->invalidateBound(myImpl->selectedPrimPath);
    }
    myImpl->suppressWatcherReload = false;
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit selectionChanged(myImpl->selectedPrimPath);
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

QString UsdViewportWidget::addReferencedAsset(const QString& assetId, const QString& usdRelPath,
                                              const QString& baseName, const QString& forwardAxis,
                                              double defaultScale) {
    if (!myImpl->stage) return QString();

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    QString cleanBase = baseName;
    cleanBase.replace(QRegularExpression("[^A-Za-z0-9_]"), "");
    if (cleanBase.isEmpty()) cleanBase = QStringLiteral("Asset");

    SdfPath primPath = SdfPath(("/World/" + cleanBase).toStdString());
    int suffix = 2;
    while (myImpl->stage->GetPrimAtPath(primPath)) {
        primPath = SdfPath(("/World/" + cleanBase + "_" + QString::number(suffix++)).toStdString());
    }

    UsdGeomXform xform = UsdGeomXform::Define(myImpl->stage, primPath);
    UsdPrim prim = xform.GetPrim();
    prim.SetCustomDataByKey(TfToken("asset_id"), VtValue(assetId.toStdString()));
    prim.SetCustomDataByKey(TfToken("forward_axis"), VtValue(forwardAxis.toStdString()));
    prim.SetCustomDataByKey(TfToken("world_state_role"), VtValue(std::string("object")));

    // Resolve path relative to user_edits.usda directory
    QString effectiveRefPath = usdRelPath;
    if (myImpl->isSceneStage && effectiveRefPath.startsWith("../../../assets/")) {
        effectiveRefPath = QStringLiteral("../../assets/") + effectiveRefPath.mid(16);
    } else if (!myImpl->isSceneStage && effectiveRefPath.startsWith("../../assets/")) {
        effectiveRefPath = QStringLiteral("../../../assets/") + effectiveRefPath.mid(13);
    } else if (effectiveRefPath.startsWith("assets/")) {
        effectiveRefPath = (myImpl->isSceneStage ? QStringLiteral("../../") : QStringLiteral("../../../")) + effectiveRefPath;
    }

    prim.GetReferences().AddReference(effectiveRefPath.toStdString());

    xform.AddTranslateOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(0.0, 0.0, 0.0));
    xform.AddRotateXYZOp(UsdGeomXformOp::PrecisionFloat).Set(GfVec3f(0.0f, 0.0f, 0.0f));
    if (std::abs(defaultScale - 1.0) > 1e-4) {
        xform.AddScaleOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(defaultScale, defaultScale, defaultScale));
    }

    // For Mixamo Z-up human assets, override /Armature rotateXYZ
    if (assetId.startsWith("character.human") || assetId.startsWith("character.clip") || assetId.contains("human")) {
        UsdPrim armOver = myImpl->stage->OverridePrim(primPath.AppendChild(TfToken("Armature")));
        UsdGeomXformable armXf(armOver);
        armXf.AddRotateXYZOp(UsdGeomXformOp::PrecisionFloat).Set(GfVec3f(0.0f, 0.0f, 0.0f), UsdTimeCode(1.0));
    }

    // Hide embedded Blender camera/cube/light/env_light if present in the referenced asset
    for (const char* hiddenChild : {"Camera", "Cube", "Light"}) {
        UsdPrim ch = myImpl->stage->GetPrimAtPath(primPath.AppendChild(TfToken(hiddenChild)));
        if (ch) {
            UsdGeomImageable(ch).MakeInvisible();
        }
    }
    UsdPrim envLight = myImpl->stage->GetPrimAtPath(primPath.AppendChild(TfToken("env_light")));
    if (envLight) {
        envLight.SetActive(false);
    }

    includeAssetInGround(myImpl->stage, prim, UsdTimeCode(myImpl->currentFrame));
    myImpl->invalidateBound(QStringLiteral("/World/Ground"));

    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    myImpl->selectedPrimPath = QString::fromStdString(primPath.GetString());
    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit selectionChanged(myImpl->selectedPrimPath);
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    return myImpl->selectedPrimPath;
}

QString UsdViewportWidget::addProceduralAsset(AssetType type, const QString& baseName) {
    if (!myImpl->stage) return QString();

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    QString cleanBase = baseName;
    cleanBase.replace(QRegularExpression("[^A-Za-z0-9_]"), "");
    if (cleanBase.isEmpty()) cleanBase = QStringLiteral("Object");

    SdfPath primPath = SdfPath(("/World/" + cleanBase).toStdString());
    int suffix = 2;
    while (myImpl->stage->GetPrimAtPath(primPath)) {
        primPath = SdfPath(("/World/" + cleanBase + "_" + QString::number(suffix++)).toStdString());
    }

    UsdGeomXform rootXf = UsdGeomXform::Define(myImpl->stage, primPath);
    rootXf.AddTranslateOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(0.0, 0.0, 0.0));
    rootXf.AddRotateXYZOp(UsdGeomXformOp::PrecisionFloat).Set(GfVec3f(0.0f, 0.0f, 0.0f));
    rootXf.AddScaleOp(UsdGeomXformOp::PrecisionDouble).Set(GfVec3d(1.0, 1.0, 1.0));

    auto setConstantColor = [](const UsdGeomGprim& gprim, const GfVec3f& c) {
        VtArray<GfVec3f> arr{c};
        UsdGeomPrimvarsAPI(gprim).CreatePrimvar(
            UsdGeomTokens->primvarsDisplayColor, SdfValueTypeNames->Color3fArray, UsdGeomTokens->constant).Set(arr);
    };

    if (type == AssetType::Tree) {
        rootXf.GetPrim().SetCustomDataByKey(TfToken("asset_id"), VtValue(std::string("prop.tree")));
        UsdGeomCylinder trunk = UsdGeomCylinder::Define(myImpl->stage, primPath.AppendChild(TfToken("Trunk")));
        trunk.CreateRadiusAttr(VtValue(0.22));
        trunk.CreateHeightAttr(VtValue(1.8));
        trunk.CreateAxisAttr(VtValue(UsdGeomTokens->y));
        trunk.AddTranslateOp().Set(GfVec3d(0.0, 0.9, 0.0));
        setConstantColor(trunk, GfVec3f(0.42f, 0.28f, 0.18f));

        UsdGeomCone canopy = UsdGeomCone::Define(myImpl->stage, primPath.AppendChild(TfToken("Canopy")));
        canopy.CreateRadiusAttr(VtValue(1.25));
        canopy.CreateHeightAttr(VtValue(2.8));
        canopy.CreateAxisAttr(VtValue(UsdGeomTokens->y));
        canopy.AddTranslateOp().Set(GfVec3d(0.0, 2.8, 0.0));
        setConstantColor(canopy, GfVec3f(0.24f, 0.52f, 0.30f));
    } else if (type == AssetType::Mountain) {
        rootXf.GetPrim().SetCustomDataByKey(TfToken("asset_id"), VtValue(std::string("environment.mountain")));
        UsdGeomCone peak = UsdGeomCone::Define(myImpl->stage, primPath.AppendChild(TfToken("Peak")));
        peak.CreateRadiusAttr(VtValue(6.0));
        peak.CreateHeightAttr(VtValue(8.5));
        peak.CreateAxisAttr(VtValue(UsdGeomTokens->y));
        peak.AddTranslateOp().Set(GfVec3d(0.0, 4.25, -18.0));
        setConstantColor(peak, GfVec3f(0.38f, 0.43f, 0.48f));
    } else if (type == AssetType::Rock) {
        rootXf.GetPrim().SetCustomDataByKey(TfToken("asset_id"), VtValue(std::string("prop.rock_group")));
        UsdGeomSphere rock = UsdGeomSphere::Define(myImpl->stage, primPath.AppendChild(TfToken("Boulder")));
        rock.CreateRadiusAttr(VtValue(0.8));
        rock.AddTranslateOp().Set(GfVec3d(0.0, 0.5, 0.0));
        rock.AddScaleOp().Set(GfVec3f(1.3f, 0.75f, 1.1f));
        setConstantColor(rock, GfVec3f(0.46f, 0.48f, 0.50f));
    } else if (type == AssetType::Mustang || type == AssetType::Pontiac) {
        const bool isMustang = (type == AssetType::Mustang);
        rootXf.GetPrim().SetCustomDataByKey(
            TfToken("asset_id"), VtValue(std::string(isMustang ? "vehicle.mustang" : "vehicle.pontiac")));
        UsdGeomCube body = UsdGeomCube::Define(myImpl->stage, primPath.AppendChild(TfToken("Body")));
        body.CreateSizeAttr(VtValue(1.0));
        body.AddTranslateOp().Set(GfVec3d(0.0, 0.58, 0.0));
        body.AddScaleOp().Set(GfVec3d(1.85, 0.65, 3.8));
        setConstantColor(body, isMustang ? GfVec3f(0.72f, 0.14f, 0.12f) : GfVec3f(0.18f, 0.36f, 0.58f));

        UsdGeomCube cabin = UsdGeomCube::Define(myImpl->stage, primPath.AppendChild(TfToken("Cabin")));
        cabin.CreateSizeAttr(VtValue(1.0));
        cabin.AddTranslateOp().Set(GfVec3d(0.0, 1.15, -0.2));
        cabin.AddScaleOp().Set(GfVec3d(1.55, 0.52, 2.0));
        setConstantColor(cabin, GfVec3f(0.16f, 0.25f, 0.32f));
    } else if (type == AssetType::Light) {
        UsdLuxSphereLight light = UsdLuxSphereLight::Define(myImpl->stage, primPath.AppendChild(TfToken("KeySphereLight")));
        light.CreateIntensityAttr(VtValue(2.5f));
        light.CreateColorAttr(VtValue(GfVec3f(1.0f, 0.92f, 0.78f)));
        light.AddTranslateOp().Set(GfVec3d(0.0, 4.0, 0.0));
    } else {
        UsdGeomCube box = UsdGeomCube::Define(myImpl->stage, primPath.AppendChild(TfToken("Geometry")));
        box.CreateSizeAttr(VtValue(1.0));
        box.AddTranslateOp().Set(GfVec3d(0.0, 0.5, 0.0));
        setConstantColor(box, GfVec3f(0.75f, 0.68f, 0.54f));
    }

    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    myImpl->selectedPrimPath = QString::fromStdString(primPath.GetString());
    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit selectionChanged(myImpl->selectedPrimPath);
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    return myImpl->selectedPrimPath;
}

bool UsdViewportWidget::userEditsEnabled() const { return myImpl->userEditsEnabled; }

void UsdViewportWidget::setUserEditsEnabled(bool enabled) {
    if (myImpl->userEditsEnabled == enabled) return;
    myImpl->userEditsEnabled = enabled;
    if (myImpl->stage && myImpl->userEditsLayer) {
        if (enabled) {
            myImpl->stage->UnmuteLayer(myImpl->userEditsLayer->GetIdentifier());
            myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        } else {
            myImpl->stage->MuteLayer(myImpl->userEditsLayer->GetIdentifier());
        }
        myImpl->invalidateBound(QString());
        myImpl->hydraDirty = true;
    }
    updateOverlayUi();
    update();
    emit stageChanged();
}

int UsdViewportWidget::userEditedPrimCount() const {
    if (!myImpl->userEditsLayer) return 0;
    QStringList paths;
    collectEditedPrimsInSpec(myImpl->userEditsLayer->GetPseudoRoot(), paths);
    paths.removeDuplicates();
    return paths.size();
}

QStringList UsdViewportWidget::orphanedUserEditPrims() const {
    QStringList orphaned;
    if (!myImpl->stage || !myImpl->userEditsLayer) return orphaned;

    QStringList edited;
    collectEditedPrimsInSpec(myImpl->userEditsLayer->GetPseudoRoot(), edited);
    edited.removeDuplicates();

    SdfLayerHandle rootLayer = myImpl->stage->GetRootLayer();
    for (const QString& p : edited) {
        if (p.startsWith("/World/Paint")) continue;
        SdfPath sdfPath(p.toStdString());
        SdfPrimSpecHandle spec = myImpl->userEditsLayer->GetPrimAtPath(sdfPath);
        if (!spec || spec->GetSpecifier() == SdfSpecifierDef) continue;

        // Check if the prim exists in the rootLayer or any of its sublayers (excluding userEditsLayer)
        bool foundInBase = static_cast<bool>(rootLayer->GetPrimAtPath(sdfPath));
        if (!foundInBase) {
            for (const std::string& subPath : rootLayer->GetSubLayerPaths()) {
                const std::string resolved = rootLayer->ComputeAbsolutePath(subPath);
                SdfLayerHandle sub = SdfLayer::FindOrOpen(resolved);
                if (sub && sub->GetPrimAtPath(sdfPath)) {
                    foundInBase = true;
                    break;
                }
            }
        }
        if (!foundInBase) {
            orphaned.append(p);
        }
    }
    return orphaned;
}

void UsdViewportWidget::clearAllUserEdits() {
    if (!myImpl->userEditsLayer) return;
    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    myImpl->userEditsLayer->ImportFromString(
        "#usda 1.0\n(\n    doc = \"PainterSpace manual user edits layer\"\n)\n\nover \"World\"\n{\n}\n");
    myImpl->userEditsLayer->Save();
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(0);
}

void UsdViewportWidget::clearSelectedPrimUserEdits() {
    if (!myImpl->userEditsLayer || myImpl->selectedPrimPath.isEmpty()) {
        clearAllUserEdits();
        return;
    }
    SdfPath targetPath(myImpl->selectedPrimPath.toStdString());
    SdfPrimSpecHandle spec = myImpl->userEditsLayer->GetPrimAtPath(targetPath);
    if (!spec) return;

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    SdfPrimSpecHandle parentSpec = spec->GetNameParent();
    if (parentSpec) {
        parentSpec->RemoveNameChild(spec);
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(myImpl->selectedPrimPath);
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::pushUndoSnapshot() {
    if (!myImpl->userEditsLayer) return;
    const QString text = captureUndoState();
    if (myImpl->undoStack.isEmpty() || myImpl->undoStack.last() != text) {
        myImpl->undoStack.append(text);
        if (myImpl->undoStack.size() > 50) myImpl->undoStack.removeFirst();
        emit undoSnapshotPushed();
    }
    myImpl->redoStack.clear();
}

bool UsdViewportWidget::canUndo() const {
    if (!myImpl->userEditsLayer || myImpl->undoStack.isEmpty()) return false;
    const QString currentText = captureUndoState();
    for (int i = myImpl->undoStack.size() - 1; i >= 0; --i) {
        if (myImpl->undoStack[i] != currentText) return true;
    }
    return false;
}

bool UsdViewportWidget::canRedo() const {
    if (!myImpl->userEditsLayer || myImpl->redoStack.isEmpty()) return false;
    const QString currentText = captureUndoState();
    for (int i = myImpl->redoStack.size() - 1; i >= 0; --i) {
        if (myImpl->redoStack[i] != currentText) return true;
    }
    return false;
}

void UsdViewportWidget::undo() {
    if (!myImpl->userEditsLayer || myImpl->undoStack.isEmpty()) return;
    const QString currentText = captureUndoState();

    QString targetText;
    bool found = false;
    while (!myImpl->undoStack.isEmpty()) {
        QString candidate = myImpl->undoStack.takeLast();
        if (candidate != currentText) {
            targetText = candidate;
            found = true;
            break;
        }
    }
    if (!found) return;

    myImpl->redoStack.append(currentText);

    myImpl->suppressWatcherReload = true;
    restoreUndoState(targetText);
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());

    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    emit selectionChanged(myImpl->selectedPrimPath);
    emit statusMessageChanged(QStringLiteral("Undid last action"));
}

void UsdViewportWidget::redo() {
    if (!myImpl->userEditsLayer || myImpl->redoStack.isEmpty()) return;
    const QString currentText = captureUndoState();

    QString nextText;
    bool found = false;
    while (!myImpl->redoStack.isEmpty()) {
        QString candidate = myImpl->redoStack.takeLast();
        if (candidate != currentText) {
            nextText = candidate;
            found = true;
            break;
        }
    }
    if (!found) return;

    myImpl->undoStack.append(currentText);
    myImpl->suppressWatcherReload = true;
    restoreUndoState(nextText);
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());

    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
    emit selectionChanged(myImpl->selectedPrimPath);
    emit statusMessageChanged(QStringLiteral("Redid last action"));
}

void UsdViewportWidget::setTool(PaintingTool tool) {
    myImpl->activeTool = tool;
    update();
}

PaintingTool UsdViewportWidget::tool() const { return myImpl->activeTool; }

void UsdViewportWidget::setBrushColor(const QColor& color) { myImpl->brushColor = color; }
void UsdViewportWidget::setBrushSize(double size) { myImpl->brushSize = size; }
void UsdViewportWidget::setBrushOpacity(double opacity) { myImpl->brushOpacity = opacity; }

void UsdViewportWidget::setBackgroundColor(const QColor& color) {
    myImpl->backgroundColor = color;
    myImpl->hydraDirty = true;
    update();
}

QColor UsdViewportWidget::backgroundColor() const { return myImpl->backgroundColor; }

void UsdViewportWidget::setGroundGridVisible(bool visible) {
    myImpl->showGroundGrid = visible;
    update();
}

bool UsdViewportWidget::groundGridVisible() const { return myImpl->showGroundGrid; }

void UsdViewportWidget::clearPaintStrokes() {
    if (!myImpl->userEditsLayer) return;
    SdfPrimSpecHandle paintSpec = myImpl->userEditsLayer->GetPrimAtPath(SdfPath("/World/Paint"));
    if (!paintSpec) return;
    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    SdfPrimSpecHandle worldSpec = paintSpec->GetNameParent();
    if (worldSpec) {
        worldSpec->RemoveNameChild(paintSpec);
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::setGenerationStatus(const QString& message, bool isError, bool isBusy) {
    myImpl->statusBannerText = message;
    myImpl->statusBannerError = isError;
    myImpl->statusBannerBusy = isBusy;
    update();
}

double UsdViewportWidget::lastFrameRenderMs() const { return myImpl->lastRenderMs; }

QMatrix4x4 UsdViewportWidget::currentViewMatrix() const {
    if (myImpl->stage && myImpl->useShotCam) {
        UsdPrim camPrim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->shotCamPath.toStdString()));
        if (camPrim && camPrim.IsA<UsdGeomCamera>()) {
            GfCamera gfCam = UsdGeomCamera(camPrim).GetCamera(UsdTimeCode(myImpl->currentFrame));
            return gfMatrixToQMatrix(gfCam.GetFrustum().ComputeViewMatrix());
        }
    }
    const float cosPitch = std::cos(static_cast<float>(myImpl->pitch));
    const QVector3D eye = myImpl->target + QVector3D(
        static_cast<float>(myImpl->distance) * cosPitch * std::sin(static_cast<float>(myImpl->yaw)),
        static_cast<float>(myImpl->distance) * std::sin(static_cast<float>(myImpl->pitch)),
        static_cast<float>(myImpl->distance) * cosPitch * std::cos(static_cast<float>(myImpl->yaw)));
    QMatrix4x4 view;
    view.lookAt(eye, myImpl->target, QVector3D(0.0f, 1.0f, 0.0f));
    return view;
}

QMatrix4x4 UsdViewportWidget::currentProjectionMatrix(double aspect) const {
    const float safeAspect = static_cast<float>(std::max(0.1, aspect));
    if (myImpl->stage && myImpl->useShotCam) {
        UsdPrim camPrim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->shotCamPath.toStdString()));
        if (camPrim && camPrim.IsA<UsdGeomCamera>()) {
            GfCamera gfCam = UsdGeomCamera(camPrim).GetCamera(UsdTimeCode(myImpl->currentFrame));
            GfFrustum frustum = gfCam.GetFrustum();
            CameraUtilConformWindow(&frustum, CameraUtilFit, safeAspect);
            return gfMatrixToQMatrix(frustum.ComputeProjectionMatrix());
        }
    }
    QMatrix4x4 proj;
    proj.perspective(45.0f, safeAspect, kFreeViewNearClip, kFreeViewFarClip);
    return proj;
}

void UsdViewportWidget::renderHydraToImage(int pixelWidth, int pixelHeight, QImage& outImage) {
    const int w = std::max(64, pixelWidth);
    const int h = std::max(64, pixelHeight);
    if (!myImpl->stage) {
        outImage = QImage(w, h, QImage::Format_RGBA8888);
        outImage.fill(myImpl->backgroundColor);
        return;
    }

    auto t0 = std::chrono::high_resolution_clock::now();
    if (!myImpl->engine) {
        UsdImagingGLEngine::Parameters params;
        params.gpuEnabled = true;
        myImpl->engine = std::make_unique<UsdImagingGLEngine>(params);
        myImpl->engine->SetEnablePresentation(false);
        myImpl->engine->SetRendererAov(HdAovTokens->color);
        myImpl->engine->SetRendererSetting(HdRenderSettingsTokens->domeLightCameraVisibility, VtValue(false));
        myImpl->engine->SetSelectionColor(GfVec4f(0.95f, 0.76f, 0.35f, 0.65f));
    }

    myImpl->engine->SetRenderBufferSize(GfVec2i(w, h));
    myImpl->engine->SetFraming(CameraUtilFraming(GfRect2i(GfVec2i(0, 0), w, h)));

    const double aspect = static_cast<double>(w) / static_cast<double>(h);
    const QMatrix4x4 qView = currentViewMatrix();
    const QMatrix4x4 qProj = currentProjectionMatrix(aspect);
    const GfMatrix4d gfView = qMatrixToGfMatrix(qView);
    const GfMatrix4d gfProj = qMatrixToGfMatrix(qProj);
    myImpl->engine->SetCameraState(gfView, gfProj);

    // Match UsdAppUtilsFrameRecorder camera headlight + default material so renders match usdrecord
    const GfMatrix4d viewInv = gfView.GetInverse();
    const GfVec3d camPos = viewInv.ExtractTranslation();
    const GfVec4f sceneAmbient(0.01f, 0.01f, 0.01f, 1.0f);
    GlfSimpleLight cameraLight(GfVec4f(float(camPos[0]), float(camPos[1]), float(camPos[2]), 1.0f));
    cameraLight.SetTransform(viewInv);
    cameraLight.SetAmbient(sceneAmbient);
    GlfSimpleLightVector lights{cameraLight};

    GlfSimpleMaterial material;
    material.SetAmbient(GfVec4f(0.2f, 0.2f, 0.2f, 1.0f));
    material.SetSpecular(GfVec4f(0.1f, 0.1f, 0.1f, 1.0f));
    material.SetShininess(32.0f);
    myImpl->engine->SetLightingState(lights, material, sceneAmbient);

    UsdImagingGLRenderParams renderParams;
    renderParams.frame = UsdTimeCode(myImpl->currentFrame);
    renderParams.complexity = 1.0f;
    renderParams.enableLighting = true;
    renderParams.enableSceneMaterials = true;
    renderParams.enableSceneLights = true;
    renderParams.showProxy = true;
    renderParams.showRender = true;
    renderParams.showGuides = false;
    // In offscreen single-AOV mode, GPU outline post-processing causes an invalid texture read crash.
    // Selection visual feedback is rendered cleanly by Qt via drawSelectionAndGizmoOverlay().
    renderParams.highlight = false;
    renderParams.colorCorrectionMode = HdxColorCorrectionTokens->disabled;
    renderParams.clearColor = GfVec4f(
        std::pow(myImpl->backgroundColor.redF(), 2.2f),
        std::pow(myImpl->backgroundColor.greenF(), 2.2f),
        std::pow(myImpl->backgroundColor.blueF(), 2.2f),
        1.0f);

    myImpl->engine->Render(myImpl->stage->GetPseudoRoot(), renderParams);

    HdRenderBuffer* rb = myImpl->engine->GetAovRenderBuffer(HdAovTokens->color);
    if (!rb) {
        outImage = QImage(w, h, QImage::Format_RGBA8888);
        outImage.fill(myImpl->backgroundColor);
        return;
    }

    rb->Resolve();
    const void* raw = rb->Map();
    const int rbW = static_cast<int>(rb->GetWidth());
    const int rbH = static_cast<int>(rb->GetHeight());
    if (outImage.width() != rbW || outImage.height() != rbH || outImage.format() != QImage::Format_RGBA8888) {
        outImage = QImage(rbW, rbH, QImage::Format_RGBA8888);
    }

    if (raw && rb->GetFormat() == HdFormatFloat16Vec4) {
        const uint8_t* lut = halfToSrgbLut();
        const uint16_t* srcHalfBits = static_cast<const uint16_t*>(raw);
        for (int y = 0; y < rbH; ++y) {
            uint8_t* dstRow = outImage.scanLine(rbH - 1 - y);
            const uint16_t* srcRow = srcHalfBits + static_cast<size_t>(y) * rbW * 4;
            for (int x = 0; x < rbW; ++x) {
                dstRow[x * 4 + 0] = lut[srcRow[x * 4 + 0]];
                dstRow[x * 4 + 1] = lut[srcRow[x * 4 + 1]];
                dstRow[x * 4 + 2] = lut[srcRow[x * 4 + 2]];
                dstRow[x * 4 + 3] = 255;
            }
        }
    } else if (raw && rb->GetFormat() == HdFormatUNorm8Vec4) {
        const uint8_t* srcBytes = static_cast<const uint8_t*>(raw);
        for (int y = 0; y < rbH; ++y) {
            uint8_t* dstRow = outImage.scanLine(rbH - 1 - y);
            std::memcpy(dstRow, srcBytes + static_cast<size_t>(y) * rbW * 4, rbW * 4);
        }
    }
    rb->Unmap();

    auto t1 = std::chrono::high_resolution_clock::now();
    myImpl->lastRenderMs = std::chrono::duration<double, std::milli>(t1 - t0).count();
}

QImage UsdViewportWidget::renderCurrentView(int width, int height) {
    const int w = width > 0 ? width : std::max(640, this->width());
    const int h = height > 0 ? height : std::max(360, this->height());
    QImage out;
    renderHydraToImage(w, h, out);
    myImpl->hydraDirty = true;
    return out;
}

QPointF UsdViewportWidget::projectWorldToScreen(const QVector3D& worldPos, const QMatrix4x4& viewProj, bool* inFront) const {
    const QVector4D clip = viewProj * QVector4D(worldPos, 1.0f);
    if (inFront) *inFront = (clip.w() > 0.05f);
    if (std::abs(clip.w()) < 1e-5f) return QPointF(-1e5, -1e5);
    const QVector3D ndc = clip.toVector3DAffine();
    const double sx = (ndc.x() * 0.5 + 0.5) * width();
    const double sy = (1.0 - (ndc.y() * 0.5 + 0.5)) * height();
    return QPointF(sx, sy);
}

bool UsdViewportWidget::screenToRay(const QPointF& screenPt, QVector3D& outOrigin, QVector3D& outDir) const {
    if (width() <= 0 || height() <= 0) return false;
    const double aspect = static_cast<double>(width()) / static_cast<double>(height());
    bool invOk = false;
    const QMatrix4x4 invVP = (currentProjectionMatrix(aspect) * currentViewMatrix()).inverted(&invOk);
    if (!invOk) return false;

    const float nx = static_cast<float>((screenPt.x() / width()) * 2.0 - 1.0);
    const float ny = static_cast<float>(1.0 - (screenPt.y() / height()) * 2.0);
    const QVector4D nearClip(nx, ny, -1.0f, 1.0f);
    const QVector4D farClip(nx, ny, 1.0f, 1.0f);
    const QVector3D nearWorld = (invVP * nearClip).toVector3DAffine();
    const QVector3D farWorld = (invVP * farClip).toVector3DAffine();
    outOrigin = nearWorld;
    outDir = (farWorld - nearWorld).normalized();
    return true;
}

bool UsdViewportWidget::screenToGroundPlane(const QPointF& screenPt, double planeY, QVector3D& outWorldPt) const {
    QVector3D orig, dir;
    if (!screenToRay(screenPt, orig, dir)) return false;
    if (std::abs(dir.y()) < 1e-4f) return false;
    const float t = (static_cast<float>(planeY) - orig.y()) / dir.y();
    if (t < 0.0f || t > 500.0f) return false;
    outWorldPt = orig + dir * t;
    return true;
}

bool UsdViewportWidget::canUseGpuPicking() const {
    // Hardware GPU picking via UsdImagingGLEngine::TestIntersection requires an active
    // graphics context bound on the current thread and dedicated ID/pick AOVs.
    // In offscreen blit mode (standard QWidget with HdAovTokens->color), no GL context is current
    // during mouse events, so attempting hardware TestIntersection causes an OpenGL/Metal crash.
    return myImpl->engine && QOpenGLContext::currentContext() != nullptr;
}

bool UsdViewportWidget::tryGpuPicking(const QPointF& screenPt, QString& outHitPrimPath, QVector3D* outHitPoint) const {
    if (!canUseGpuPicking() || width() <= 0 || height() <= 0 || !myImpl->stage) return false;

    const double nx = (screenPt.x() / width()) * 2.0 - 1.0;
    const double ny = 1.0 - (screenPt.y() / height()) * 2.0;
    const double aspect = static_cast<double>(width()) / static_cast<double>(height());
    const GfMatrix4d gfView = qMatrixToGfMatrix(currentViewMatrix());
    const GfMatrix4d gfProj = qMatrixToGfMatrix(currentProjectionMatrix(aspect));

    GfFrustum frustum;
    if (myImpl->stage && myImpl->useShotCam) {
        UsdPrim camPrim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->shotCamPath.toStdString()));
        if (camPrim && camPrim.IsA<UsdGeomCamera>()) {
            GfCamera gfCam = UsdGeomCamera(camPrim).GetCamera(UsdTimeCode(myImpl->currentFrame));
            frustum = gfCam.GetFrustum();
            CameraUtilConformWindow(&frustum, CameraUtilFit, aspect);
        } else {
            frustum.SetPositionAndRotationFromMatrix(gfView.GetInverse());
            frustum.SetPerspective(45.0, true, aspect, 1.0, 1000.0);
        }
    } else {
        frustum.SetPositionAndRotationFromMatrix(gfView.GetInverse());
        frustum.SetPerspective(45.0, true, aspect, 0.1, 1000.0);
    }
    const GfFrustum pickFrustum = frustum.ComputeNarrowedFrustum(GfVec2d(nx, ny), GfVec2d(8.0 / width(), 8.0 / height()));

    UsdImagingGLRenderParams pickParams;
    pickParams.frame = UsdTimeCode(myImpl->currentFrame);
    pickParams.showProxy = true;
    pickParams.showRender = true;
    pickParams.showGuides = false;

    GfVec3d hitPoint(0.0), hitNormal(0.0);
    SdfPath hitPrimPath, hitInstancerPath;
    int hitInstanceIdx = -1;
    const bool hit = myImpl->engine->TestIntersection(
        pickFrustum.ComputeViewMatrix(), pickFrustum.ComputeProjectionMatrix(),
        myImpl->stage->GetPseudoRoot(), pickParams,
        &hitPoint, &hitNormal, &hitPrimPath, &hitInstancerPath, &hitInstanceIdx);

    if (hit && !hitPrimPath.IsEmpty()) {
        const SdfPath resolved = resolveEditablePrimPath(myImpl->stage, hitPrimPath);
        outHitPrimPath = QString::fromStdString(resolved.GetString());
        if (outHitPoint) {
            *outHitPoint = QVector3D(float(hitPoint[0]), float(hitPoint[1]), float(hitPoint[2]));
        }
        return true;
    }
    return false;
}

bool UsdViewportWidget::tryCpuRayPick(const QPointF& screenPt, QString& outHitPrimPath, QVector3D* outHitPoint) const {
    if (!myImpl->stage) return false;

    QVector3D rayOrig, rayDir;
    if (!screenToRay(screenPt, rayOrig, rayDir)) return false;

    const GfRay ray(
        GfVec3d(rayOrig.x(), rayOrig.y(), rayOrig.z()),
        GfVec3d(rayDir.x(), rayDir.y(), rayDir.z()));

    UsdPrim worldPrim = myImpl->stage->GetPrimAtPath(SdfPath("/World"));
    if (!worldPrim) worldPrim = myImpl->stage->GetPseudoRoot();

    const UsdTimeCode time(myImpl->currentFrame);

    double closestDist = std::numeric_limits<double>::max();
    double bestFarDist = std::numeric_limits<double>::max();
    double bestSize = std::numeric_limits<double>::max();
    SdfPath bestPrimPath;

    // Large environment bounds often surround smaller visible assets. When their
    // ray intervals overlap, prefer the smaller bound instead of the front face
    // of a building's mostly empty bounding box.
    const auto considerBox = [&](const GfBBox3d& box, const SdfPath& path) {
        const GfRange3d range = box.ComputeAlignedRange();
        if (range.IsEmpty()) return false;
        double dMin = 0.0, dMax = 0.0;
        if (!ray.Intersect(box, &dMin, &dMax) || dMax < 0.0) return false;
        const double nearDist = std::max(0.0, dMin);
        const double size = range.GetSize().GetLength();
        const bool overlap = nearDist <= bestFarDist && closestDist <= dMax;
        const bool substantiallySmaller = size * 3.0 < bestSize;
        const bool substantiallyLarger = bestSize * 3.0 < size;
        if (bestPrimPath.IsEmpty() ||
            (path == bestPrimPath && nearDist < closestDist) ||
            (path != bestPrimPath && overlap && substantiallySmaller) ||
            (path != bestPrimPath && !(overlap && substantiallyLarger) && nearDist < closestDist)) {
            closestDist = nearDist;
            bestFarDist = dMax;
            bestSize = size;
            bestPrimPath = path;
        }
        return true;
    };

    myImpl->loadManifestCollidersIfNeeded();

    for (const UsdPrim& child : worldPrim.GetChildren()) {
        const std::string nameStr = child.GetName().GetString();
        if (nameStr == "Zones") continue;

        UsdGeomImageable img(child);
        if (img) {
            TfToken vis = img.ComputeVisibility(UsdTimeCode(myImpl->currentFrame));
            if (vis == UsdGeomTokens->invisible) continue;
        } else if (!child.IsActive()) {
            continue;
        }

        if (nameStr == "Paint") {
            for (const UsdPrim& stroke : child.GetChildren()) {
                considerBox(myImpl->cachedWorldBound(stroke), stroke.GetPath());
            }
            continue;
        }

        // Check if child matches an ingested asset with collider boxes
        QString assetId;
        VtValue assetIdVal = child.GetCustomDataByKey(TfToken("asset_id"));
        if (assetIdVal.IsHolding<std::string>()) {
            assetId = QString::fromStdString(assetIdVal.UncheckedGet<std::string>());
        }
        if (assetId.isEmpty()) {
            assetId = QString::fromStdString(nameStr);
        }

        QVector<Impl::ColliderBox> colliders = myImpl->manifestColliders.value(assetId);
        if (colliders.isEmpty()) {
            colliders = myImpl->manifestColliders.value(assetId.toLower());
        }
        if (colliders.isEmpty()) {
            for (const UsdPrim& sub : child.GetChildren()) {
                VtValue subAssetId = sub.GetCustomDataByKey(TfToken("asset_id"));
                if (subAssetId.IsHolding<std::string>()) {
                    colliders = myImpl->manifestColliders.value(QString::fromStdString(subAssetId.UncheckedGet<std::string>()));
                    if (!colliders.isEmpty()) break;
                }
            }
        }

        if (!colliders.isEmpty()) {
            UsdGeomXformable xf(child);
            GfMatrix4d l2w = xf ? xf.ComputeLocalToWorldTransform(time) : GfMatrix4d(1.0);
            bool hitCollider = false;
            for (const auto& box : colliders) {
                GfRange3d localRng(box.center - box.size * 0.5, box.center + box.size * 0.5);
                GfBBox3d obb(localRng, l2w);
                hitCollider = considerBox(obb, child.GetPath()) || hitCollider;
            }
            if (hitCollider) continue;
        }

        considerBox(myImpl->cachedWorldBound(child), child.GetPath());
    }

    if (!bestPrimPath.IsEmpty()) {
        const SdfPath resolved = resolveEditablePrimPath(myImpl->stage, bestPrimPath);
        outHitPrimPath = QString::fromStdString(resolved.GetString());
        if (outHitPoint) {
            const GfVec3d hitPt = ray.GetPoint(closestDist);
            *outHitPoint = QVector3D(float(hitPt[0]), float(hitPt[1]), float(hitPt[2]));
        }
        return true;
    }
    return false;
}

void UsdViewportWidget::drawGridOverlay(QPainter& painter, const QMatrix4x4& viewProj) const {
    if (!myImpl->showGroundGrid) return;
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    const int extent = 24;
    for (int i = -extent; i <= extent; ++i) {
        const bool isMajor = (i % 4 == 0);
        const bool isCenter = (i == 0);
        if (isCenter) {
            painter.setPen(QPen(QColor(115, 181, 218, 120), 1.4));
        } else if (isMajor) {
            painter.setPen(QPen(QColor(160, 175, 190, 55), 1.0));
        } else {
            painter.setPen(QPen(QColor(140, 155, 170, 28), 1.0));
        }

        // Line along X at Z = i
        bool f1 = false, f2 = false;
        const QPointF p1 = projectWorldToScreen(QVector3D(-extent, 0.0f, float(i)), viewProj, &f1);
        const QPointF p2 = projectWorldToScreen(QVector3D(extent, 0.0f, float(i)), viewProj, &f2);
        if (f1 && f2) painter.drawLine(p1, p2);

        // Line along Z at X = i
        const QPointF q1 = projectWorldToScreen(QVector3D(float(i), 0.0f, -extent), viewProj, &f1);
        const QPointF q2 = projectWorldToScreen(QVector3D(float(i), 0.0f, extent), viewProj, &f2);
        if (f1 && f2) painter.drawLine(q1, q2);
    }
    painter.restore();
}

void UsdViewportWidget::drawOrbitCenterOverlay(QPainter& painter, const QMatrix4x4& viewProj) const {
    if (!myImpl->fixedOrbitCenter || myImpl->useShotCam) return;
    // Scale the navigation marker with viewing distance so it stays legible.
    // Its center and vertical direction remain fixed in world space.
    const float radius = static_cast<float>(myImpl->distance * 0.025);
    constexpr int ringSegments = 48;
    constexpr double fullTurn = 6.28318530717958647692;
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);
    painter.setPen(QPen(QColor("#5ec27e"), 1.5));
    for (int i = 0; i < ringSegments; ++i) {
        const double a = fullTurn * i / ringSegments;
        const double b = fullTurn * (i + 1) / ringSegments;
        bool frontA = false, frontB = false;
        const QPointF start = projectWorldToScreen(myImpl->target + QVector3D(
            radius * std::cos(a), 0.0f, radius * std::sin(a)), viewProj, &frontA);
        const QPointF end = projectWorldToScreen(myImpl->target + QVector3D(
            radius * std::cos(b), 0.0f, radius * std::sin(b)), viewProj, &frontB);
        if (frontA && frontB) painter.drawLine(start, end);
    }
    bool frontA = false, frontB = false;
    const QPointF start = projectWorldToScreen(myImpl->target - QVector3D(0, radius * 2, 0), viewProj, &frontA);
    const QPointF end = projectWorldToScreen(myImpl->target + QVector3D(0, radius * 2, 0), viewProj, &frontB);
    painter.setPen(QPen(QColor("#5ec27e"), 1.5, Qt::DashLine));
    if (frontA && frontB) painter.drawLine(start, end);
    painter.restore();
}

void UsdViewportWidget::drawSelectionAndGizmoOverlay(QPainter& painter, const QMatrix4x4& viewProj) const {
    if (!myImpl->stage) return;

    if (myImpl->selectedAimTarget) {
        painter.save();
        painter.setRenderHint(QPainter::Antialiasing, true);
        const QVector3D origin = resolvedAimTargetPosition();
        bool originInFront = false;
        const QPointF originPt = projectWorldToScreen(origin, viewProj, &originInFront);
        if (originInFront) {
            const float axisLen = 1.6f;
            const QVector3D axisEnds[3] = {
                origin + QVector3D(axisLen, 0.0f, 0.0f),
                origin + QVector3D(0.0f, axisLen, 0.0f),
                origin + QVector3D(0.0f, 0.0f, axisLen)
            };
            const QColor axisColors[3] = {
                QColor("#e85d4a"), // +X Red
                QColor("#5ec27e"), // +Y Green
                QColor("#5b9be6")  // +Z Blue
            };
            const char* axisNames[3] = {"X", "Y", "Z"};

            for (int a = 0; a < 3; ++a) {
                bool endFront = false;
                const QPointF endPt = projectWorldToScreen(axisEnds[a], viewProj, &endFront);
                if (!endFront) continue;
                const bool active = (myImpl->activeGizmoAxis == a);
                painter.setPen(QPen(active ? QColor("#ffffff") : axisColors[a], active ? 3.5 : 2.5));
                painter.drawLine(originPt, endPt);
                painter.setBrush(axisColors[a]);
                painter.drawEllipse(endPt, 5.5, 5.5);
                painter.setPen(axisColors[a].lighter(135));
                painter.drawText(endPt + QPointF(7.0, -5.0), QString::fromLatin1(axisNames[a]));
            }
            painter.setBrush(QColor("#22c55e"));
            painter.setPen(QPen(QColor("#14532d"), 1.8));
            painter.drawEllipse(originPt, 6.0, 6.0);
        }
        painter.restore();
        return;
    }

    if (myImpl->selectedPrimPath.isEmpty()) return;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim) return;

    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    // 1. Draw world-aligned bounding box for selected prim
    GfBBox3d bbox = myImpl->cachedWorldBound(prim);
    GfRange3d range = bbox.ComputeAlignedRange();
    if (!range.IsEmpty()) {
        const GfVec3d mn = range.GetMin();
        const GfVec3d mx = range.GetMax();
        const QVector3D corners[8] = {
            QVector3D(mn[0], mn[1], mn[2]), QVector3D(mx[0], mn[1], mn[2]),
            QVector3D(mx[0], mn[1], mx[2]), QVector3D(mn[0], mn[1], mx[2]),
            QVector3D(mn[0], mx[1], mn[2]), QVector3D(mx[0], mx[1], mn[2]),
            QVector3D(mx[0], mx[1], mx[2]), QVector3D(mn[0], mx[1], mx[2]),
        };
        const int edges[12][2] = {
            {0,1},{1,2},{2,3},{3,0},
            {4,5},{5,6},{6,7},{7,4},
            {0,4},{1,5},{2,6},{3,7}
        };
        painter.setPen(QPen(QColor(240, 196, 122, 210), 1.6, Qt::DashLine));
        for (const auto& e : edges) {
            bool f1 = false, f2 = false;
            const QPointF a = projectWorldToScreen(corners[e[0]], viewProj, &f1);
            const QPointF b = projectWorldToScreen(corners[e[1]], viewProj, &f2);
            if (f1 && f2) painter.drawLine(a, b);
        }
    }

    painter.restore();
}

int UsdViewportWidget::hitTestGizmoAxis(const QPointF& screenPt) const {
    if (!myImpl->stage || !myImpl->selectedAimTarget) return -1;
    const QVector3D origin = resolvedAimTargetPosition();
    const double aspect = static_cast<double>(width()) / static_cast<double>(std::max(1, height()));
    const QMatrix4x4 vp = currentProjectionMatrix(aspect) * currentViewMatrix();
    bool originFront = false;
    const QPointF originPt = projectWorldToScreen(origin, vp, &originFront);
    if (!originFront) return -1;

    const float axisLen = 1.6f;
    const QVector3D axisEnds[3] = {
        origin + QVector3D(axisLen, 0.0f, 0.0f),
        origin + QVector3D(0.0f, axisLen, 0.0f),
        origin + QVector3D(0.0f, 0.0f, axisLen)
    };
    for (int a = 0; a < 3; ++a) {
        bool endFront = false;
        const QPointF endPt = projectWorldToScreen(axisEnds[a], vp, &endFront);
        if (!endFront) continue;
        // Distance to segment originPt -> endPt
        const QPointF seg = endPt - originPt;
        const double lenSq = QPointF::dotProduct(seg, seg);
        double t = lenSq > 1e-4 ? QPointF::dotProduct(screenPt - originPt, seg) / lenSq : 0.0;
        t = std::clamp(t, 0.0, 1.0);
        const QPointF closest = originPt + seg * t;
        if (QLineF(screenPt, closest).length() <= 11.0) {
            return a;
        }
    }
    return -1;
}

bool UsdViewportWidget::renameActorLabel(const QString& primPath, const QString& newLabel) {
    if (!myImpl->stage || primPath.isEmpty() || newLabel.trimmed().isEmpty()) return false;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(primPath.toStdString()));
    if (!prim) return false;

    pushUndoSnapshot();
    myImpl->suppressWatcherReload = true;
    if (myImpl->userEditsLayer) {
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }
    prim.SetCustomDataByKey(TfToken("label"), VtValue(newLabel.trimmed().toStdString()));
    if (myImpl->userEditsLayer) {
        myImpl->userEditsLayer->Save();
    }
    myImpl->suppressWatcherReload = false;
    myImpl->hydraDirty = true;
    update();
    emit stageChanged();
    return true;
}

void UsdViewportWidget::drawActorNameTags(QPainter& painter, const QMatrix4x4& viewProj) const {
    if (!myImpl->stage) return;
    const QVector<UsdPrimItem> items = primList();
    if (items.isEmpty()) return;

    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    const QString selected = selectedPrimPath();

    for (const UsdPrimItem& item : items) {
        if (item.actorId.isEmpty() && item.actorLabel.isEmpty()) continue;

        UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(item.path.toStdString()));
        if (!prim || !item.visible) continue;

        GfBBox3d bbox = myImpl->cachedWorldBound(prim);
        GfRange3d range = bbox.ComputeAlignedRange();
        QVector3D topCenter;
        if (!range.IsEmpty()) {
            const GfVec3d mx = range.GetMax();
            const GfVec3d mn = range.GetMin();
            topCenter = QVector3D(float((mn[0] + mx[0]) * 0.5), float(mx[1] + 0.25), float((mn[2] + mx[2]) * 0.5));
        } else {
            UsdGeomXformable xf(prim);
            GfMatrix4d xfMat = xf ? xf.ComputeLocalToWorldTransform(UsdTimeCode(myImpl->currentFrame)) : GfMatrix4d(1.0);
            GfVec3d tr = xfMat.ExtractTranslation();
            topCenter = QVector3D(float(tr[0]), float(tr[1] + 1.85), float(tr[2]));
        }

        bool inFront = false;
        QPointF screenPt = projectWorldToScreen(topCenter, viewProj, &inFront);
        if (!inFront || screenPt.x() < -50 || screenPt.x() > width() + 50 ||
            screenPt.y() < -50 || screenPt.y() > height() + 50) {
            continue;
        }

        const bool isSelected = (item.path == selected);
        const QString displayName = item.actorLabel.isEmpty() ? item.actorId : item.actorLabel;

        QFont font = painter.font();
        font.setPointSize(11);
        font.setBold(isSelected);
        painter.setFont(font);

        QFontMetrics fm(font);
        const int textW = fm.horizontalAdvance(displayName);
        const int pillW = textW + 28;
        const int pillH = 22;
        const QRectF pillRect(screenPt.x() - pillW / 2.0, screenPt.y() - pillH, pillW, pillH);

        // Background pill
        painter.setPen(QPen(isSelected ? QColor("#ffffff") : item.identityColor, isSelected ? 2.0 : 1.2));
        painter.setBrush(QColor(22, 27, 34, 215));
        painter.drawRoundedRect(pillRect, 6.0, 6.0);

        // Color dot
        painter.setPen(Qt::NoPen);
        painter.setBrush(item.identityColor);
        painter.drawEllipse(QPointF(pillRect.left() + 9, pillRect.center().y()), 4.0, 4.0);

        // Text
        painter.setPen(isSelected ? QColor("#ffffff") : QColor("#e6edf3"));
        painter.drawText(QRectF(pillRect.left() + 18, pillRect.top(), textW + 4, pillH),
                         Qt::AlignVCenter | Qt::AlignLeft, displayName);
    }

    painter.restore();
}

void UsdViewportWidget::drawOrientationGizmo(QPainter& painter) const {
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    const QRectF gizmoRect(width() - 132.0, 12.0, 120.0, 120.0);
    painter.setBrush(QColor(21, 25, 30, 200));
    painter.setPen(QPen(QColor(52, 62, 75), 1.0));
    painter.drawRoundedRect(gizmoRect, 10.0, 10.0);

    // Project world directions through the camera's horizontal right vector.
    // This keeps the compass readable even when looking level at the horizon.
    const QMatrix4x4 cameraToWorld = currentViewMatrix().inverted();
    const QVector3D cameraRight = cameraToWorld.mapVector(QVector3D(1, 0, 0));
    QVector2D right(cameraRight.x(), cameraRight.z());
    if (right.lengthSquared() <= 1e-8f) {
        const QVector3D cameraUp = cameraToWorld.mapVector(QVector3D(0, 1, 0));
        right = QVector2D(-cameraUp.z(), cameraUp.x());
    }
    if (right.lengthSquared() > 1e-8f) {
        const QVector2D screenRight = right.normalized();
        const QVector2D screenUp(screenRight.y(), -screenRight.x());
        const QPointF center(gizmoRect.center().x(), gizmoRect.top() + 57.0);
        const QVector2D directions[] = {{0, -1}, {1, 0}, {0, 1}, {-1, 0}};
        const char* labels[] = {"N", "E", "S", "W"};
        for (int i = 0; i < 4; ++i) {
            const double x = QVector2D::dotProduct(directions[i], screenRight);
            const double y = QVector2D::dotProduct(directions[i], screenUp);
            const QPointF tip = center + QPointF(x * 35.0, -y * 35.0);
            const QColor color = i == 0 ? QColor("#f87171") : QColor("#d7e4ef");
            painter.setPen(QPen(color, i == 0 ? 2.2 : 1.3));
            painter.drawLine(center, tip);
            painter.setBrush(i == 0 ? QColor("#7f3030") : QColor("#263544"));
            painter.drawEllipse(tip, 9.0, 9.0);
            painter.setPen(color);
            painter.drawText(QRectF(tip.x() - 9.0, tip.y() - 9.0, 18.0, 18.0),
                             Qt::AlignCenter, QString::fromLatin1(labels[i]));
        }
        painter.setPen(QColor("#7d93a5"));
        painter.drawText(gizmoRect.adjusted(0, 99, 0, -3), Qt::AlignHCenter,
                         QStringLiteral("N −Z  ·  E +X"));
    }
    painter.restore();
}

void UsdViewportWidget::drawStatusBanner(QPainter& painter) const {
    if (myImpl->statusBannerText.isEmpty()) return;
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);
    const QRectF bannerRect(12.0, 12.0, std::min(560.0, width() - 120.0), 28.0);
    if (myImpl->statusBannerError) {
        painter.setBrush(QColor(95, 32, 32, 230));
        painter.setPen(QPen(QColor("#e38e83"), 1.2));
    } else if (myImpl->statusBannerBusy) {
        painter.setBrush(QColor(45, 65, 88, 230));
        painter.setPen(QPen(QColor("#8ad0f5"), 1.2));
    } else {
        painter.setBrush(QColor(34, 59, 46, 225));
        painter.setPen(QPen(QColor("#7ed9a6"), 1.2));
    }
    painter.drawRoundedRect(bannerRect, 6.0, 6.0);
    painter.setPen(QColor("#f5f7f9"));
    QFont f = painter.font();
    f.setPixelSize(11);
    f.setBold(true);
    painter.setFont(f);
    painter.drawText(bannerRect.adjusted(10, 0, -10, 0), Qt::AlignVCenter | Qt::AlignLeft,
                     myImpl->statusBannerText);
    painter.restore();
}

void UsdViewportWidget::drawCameraFrustumAndPath(QPainter& painter, const QMatrix4x4& viewProj) const {
    if (!myImpl->stage || !myImpl->cameraTools) return;
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    const CameraRigData& rig = myImpl->cameraTools->rigData();
    CameraRigEvaluator evaluator(myImpl->fps);

    // 1. Draw 3D spline curve path
    if (rig.pathDrawn && rig.points.size() >= 2) {
        const QVector<QVector3D> polyline = evaluator.samplePolyline(rig, 100);
        painter.setPen(QPen(QColor("#38bdf8"), 2.2));
        for (int i = 0; i < polyline.size() - 1; ++i) {
            bool f1 = false, f2 = false;
            const QPointF p1 = projectWorldToScreen(polyline[i], viewProj, &f1);
            const QPointF p2 = projectWorldToScreen(polyline[i + 1], viewProj, &f2);
            if (f1 && f2) {
                painter.drawLine(p1, p2);
            }
        }

        // Draw time tick marks every 0.5s along path
        const double duration = std::max(0.5, rig.pathEndS - rig.pathStartS);
        const int numTicks = static_cast<int>(std::floor(duration / 0.5));
        QFont tickFont = painter.font();
        tickFont.setPixelSize(10);
        painter.setFont(tickFont);
        for (int i = 0; i <= numTicks; ++i) {
            const double t = rig.pathStartS + i * 0.5;
            CameraSample sample = evaluator.evaluate(rig, t);
            bool f = false;
            QPointF tickPt = projectWorldToScreen(sample.position, viewProj, &f);
            if (f) {
                painter.setBrush(QColor("#38bdf8"));
                painter.setPen(QPen(QColor("#0f172a"), 1.0));
                painter.drawEllipse(tickPt, 3.5, 3.5);
                painter.setPen(QColor("#bae6fd"));
                painter.drawText(tickPt + QPointF(5, -4), QString("%1s").arg(i * 0.5, 0, 'f', 1));
            }
        }
    }

    // 2. Draw control points (path joints)
    const int selIdx = myImpl->cameraTools->selectedPointIndex();
    const int numPts = rig.pathDrawn ? rig.points.size() : 0;
    for (int i = 0; i < numPts; ++i) {
        bool f = false;
        const QPointF pt = projectWorldToScreen(rig.points[i].position, viewProj, &f);
        if (!f) continue;
        const bool isSelected = (i == selIdx);

        QString labelText;
        if (numPts > 1) {
            if (i == 0) labelText = QString("Joint %1 (Start)").arg(i + 1);
            else if (i == numPts - 1) labelText = QString("Joint %1 (End)").arg(i + 1);
            else labelText = QString("Joint %1").arg(i + 1);
        } else {
            labelText = QString("Joint 1");
        }

        if (isSelected) {
            painter.setBrush(Qt::NoBrush);
            painter.setPen(QPen(QColor(251, 191, 36, 140), 1.5, Qt::DashLine));
            painter.drawEllipse(pt, 11.0, 11.0);

            painter.setPen(QPen(QColor("#f59e0b"), 2.2));
            painter.setBrush(QColor("#fbbf24"));
            painter.drawEllipse(pt, 6.0, 6.0);

            QFont jFont = painter.font();
            jFont.setPixelSize(10);
            jFont.setBold(true);
            painter.setFont(jFont);
            const QString selLabel = labelText + " (Selected)";
            const int tw = painter.fontMetrics().horizontalAdvance(selLabel);
            const QRectF jBadge(pt.x() + 9, pt.y() - 14, tw + 8, 16);
            painter.setBrush(QColor(15, 23, 42, 220));
            painter.setPen(QPen(QColor("#fbbf24"), 1.2));
            painter.drawRoundedRect(jBadge, 4, 4);
            painter.setPen(QColor("#ffffff"));
            painter.drawText(jBadge, Qt::AlignCenter, selLabel);
        } else {
            painter.setPen(QPen(QColor("#0284c7"), 1.5));
            painter.setBrush(QColor("#38bdf8"));
            painter.drawEllipse(pt, 4.5, 4.5);
            painter.setPen(QColor("#cbd5e1"));
            QFont jFont = painter.font();
            jFont.setPixelSize(9);
            painter.setFont(jFont);
            painter.drawText(pt + QPointF(7, -3), labelText);
        }
    }

    // 3. Draw Camera 3D Mesh Representation & Frustum at current frame
    CameraSample curSample = evaluator.evaluate(rig, myImpl->currentFrame / myImpl->fps);
    const QVector3D camPos = curSample.position;
    const QVector3D rotDeg = curSample.rotationDeg;
    const bool isCamSelected = (myImpl->selectedPrimPath == myImpl->shotCamPath ||
                                myImpl->selectedPrimPath == QStringLiteral("/World/ShotCam"));

    QMatrix4x4 camMat;
    camMat.rotate(rotDeg.y(), 0.0f, 1.0f, 0.0f);
    camMat.rotate(rotDeg.x(), 1.0f, 0.0f, 0.0f);
    camMat.rotate(rotDeg.z(), 0.0f, 0.0f, 1.0f);

    const QVector3D forward = camMat.mapVector(QVector3D(0.0f, 0.0f, -1.0f));
    const QVector3D right = camMat.mapVector(QVector3D(1.0f, 0.0f, 0.0f));
    const QVector3D up = camMat.mapVector(QVector3D(0.0f, 1.0f, 0.0f));

    // A. 3D Camera Body (Chassis) - smaller, sleek cinema camera body
    const float bodyW = 0.26f;
    const float bodyH = 0.17f;
    const float bodyD = 0.22f;
    const QVector3D halfW = right * (bodyW * 0.5f);
    const QVector3D halfH = up * (bodyH * 0.5f);
    const QVector3D fCenter = camPos;
    const QVector3D bCenter = camPos - forward * bodyD;

    // 8 vertices of camera body box
    const QVector3D vF_BL = fCenter - halfW - halfH;
    const QVector3D vF_BR = fCenter + halfW - halfH;
    const QVector3D vF_TR = fCenter + halfW + halfH;
    const QVector3D vF_TL = fCenter - halfW + halfH;
    const QVector3D vB_BL = bCenter - halfW - halfH;
    const QVector3D vB_BR = bCenter + halfW - halfH;
    const QVector3D vB_TR = bCenter + halfW + halfH;
    const QVector3D vB_TL = bCenter - halfW + halfH;

    bool fF_BL=false, fF_BR=false, fF_TR=false, fF_TL=false;
    bool fB_BL=false, fB_BR=false, fB_TR=false, fB_TL=false;
    const QPointF pF_BL = projectWorldToScreen(vF_BL, viewProj, &fF_BL);
    const QPointF pF_BR = projectWorldToScreen(vF_BR, viewProj, &fF_BR);
    const QPointF pF_TR = projectWorldToScreen(vF_TR, viewProj, &fF_TR);
    const QPointF pF_TL = projectWorldToScreen(vF_TL, viewProj, &fF_TL);
    const QPointF pB_BL = projectWorldToScreen(vB_BL, viewProj, &fB_BL);
    const QPointF pB_BR = projectWorldToScreen(vB_BR, viewProj, &fB_BR);
    const QPointF pB_TR = projectWorldToScreen(vB_TR, viewProj, &fB_TR);
    const QPointF pB_TL = projectWorldToScreen(vB_TL, viewProj, &fB_TL);

    const QColor bodyFill = isCamSelected ? QColor(36, 44, 58, 230) : QColor(21, 27, 36, 220);
    const QColor bodyEdge = isCamSelected ? QColor("#fbbf24") : QColor("#58a6ff");
    painter.setPen(QPen(bodyEdge, isCamSelected ? 1.8 : 1.3));

    // Draw camera body faces
    if (fF_TL && fF_TR && fB_TR && fB_TL) {
        painter.setBrush(bodyFill.lighter(125));
        painter.drawPolygon(QPolygonF({pF_TL, pF_TR, pB_TR, pB_TL})); // Top face
    }
    if (fF_BL && fF_BR && fB_BR && fB_BL) {
        painter.setBrush(bodyFill.darker(115));
        painter.drawPolygon(QPolygonF({pF_BL, pF_BR, pB_BR, pB_BL})); // Bottom face
    }
    if (fF_BL && fF_TL && fB_TL && fB_BL) {
        painter.setBrush(bodyFill);
        painter.drawPolygon(QPolygonF({pF_BL, pF_TL, pB_TL, pB_BL})); // Left face
    }
    if (fF_BR && fF_TR && fB_TR && fB_BR) {
        painter.setBrush(bodyFill.darker(108));
        painter.drawPolygon(QPolygonF({pF_BR, pF_TR, pB_TR, pB_BR})); // Right face
    }
    if (fF_BL && fF_BR && fF_TR && fF_TL) {
        painter.setBrush(bodyFill.lighter(112));
        painter.drawPolygon(QPolygonF({pF_BL, pF_BR, pF_TR, pF_TL})); // Front face
    }

    // B. Film magazine reels on top of camera body
    const QVector3D reelCenter1 = fCenter - forward * (bodyD * 0.35f) + up * (bodyH * 0.5f + 0.055f);
    const QVector3D reelCenter2 = fCenter - forward * (bodyD * 0.70f) + up * (bodyH * 0.5f + 0.055f);
    const float reelR = 0.055f;
    for (const QVector3D& rc : {reelCenter1, reelCenter2}) {
        QPolygonF reelPoly;
        bool allReelOk = true;
        for (int k = 0; k < 8; ++k) {
            const double ang = k * (2.0 * 3.14159265358979323846 / 8.0);
            const QVector3D pt = rc + (forward * std::cos(ang) + up * std::sin(ang)) * reelR;
            bool ok = false;
            const QPointF sc = projectWorldToScreen(pt, viewProj, &ok);
            if (!ok) { allReelOk = false; break; }
            reelPoly << sc;
        }
        if (allReelOk) {
            painter.setBrush(bodyFill.darker(120));
            painter.setPen(QPen(bodyEdge, 1.2));
            painter.drawPolygon(reelPoly);
            bool hubOk = false;
            const QPointF hubPt = projectWorldToScreen(rc, viewProj, &hubOk);
            if (hubOk) {
                painter.setBrush(isCamSelected ? QColor("#fbbf24") : QColor("#38bdf8"));
                painter.drawEllipse(hubPt, 2.5, 2.5);
            }
        }
    }

    // C. Lens barrel & front rim
    const QVector3D lensApex = fCenter + forward * 0.08f;
    const float lensR = 0.055f;
    QPolygonF lensRim;
    bool allLensOk = true;
    for (int k = 0; k < 8; ++k) {
        const double ang = k * (2.0 * 3.14159265358979323846 / 8.0);
        const QVector3D pt = lensApex + (right * std::cos(ang) + up * std::sin(ang)) * lensR;
        bool ok = false;
        const QPointF sc = projectWorldToScreen(pt, viewProj, &ok);
        if (!ok) { allLensOk = false; break; }
        lensRim << sc;
    }
    if (allLensOk) {
        painter.setBrush(QColor(24, 32, 44, 240));
        painter.setPen(QPen(QColor("#38bdf8"), 1.4));
        painter.drawPolygon(lensRim);
    }

    // D. Compact Frustum Pyramid (frustumDist = 0.9m)
    const float frustumDist = 0.9f;
    const float vertFovRad = static_cast<float>(2.0 * std::atan(curSample.verticalAperture / (2.0 * curSample.focalLength)));
    const float frustumH = 2.0f * frustumDist * std::tan(vertFovRad * 0.5f);
    const float aspect = static_cast<float>(curSample.horizontalAperture / curSample.verticalAperture);
    const float frustumW = frustumH * aspect;

    const QVector3D centerFar = lensApex + forward * frustumDist;
    const QVector3D cTL = centerFar - right * (frustumW * 0.5f) + up * (frustumH * 0.5f);
    const QVector3D cTR = centerFar + right * (frustumW * 0.5f) + up * (frustumH * 0.5f);
    const QVector3D cBR = centerFar + right * (frustumW * 0.5f) - up * (frustumH * 0.5f);
    const QVector3D cBL = centerFar - right * (frustumW * 0.5f) - up * (frustumH * 0.5f);

    bool fApex = false, fTL = false, fTR = false, fBR = false, fBL = false;
    const QPointF pApex = projectWorldToScreen(lensApex, viewProj, &fApex);
    const QPointF pTL = projectWorldToScreen(cTL, viewProj, &fTL);
    const QPointF pTR = projectWorldToScreen(cTR, viewProj, &fTR);
    const QPointF pBR = projectWorldToScreen(cBR, viewProj, &fBR);
    const QPointF pBL = projectWorldToScreen(cBL, viewProj, &fBL);

    if (fApex) {
        // Frustum rays from lens to 4 corners
        painter.setPen(QPen(isCamSelected ? QColor(251, 191, 36, 180) : QColor(56, 189, 248, 150), 1.2, Qt::DashLine));
        if (fTL) painter.drawLine(pApex, pTL);
        if (fTR) painter.drawLine(pApex, pTR);
        if (fBR) painter.drawLine(pApex, pBR);
        if (fBL) painter.drawLine(pApex, pBL);

        // Frustum far framing rectangle
        if (fTL && fTR && fBR && fBL) {
            painter.setPen(QPen(isCamSelected ? QColor(251, 191, 36, 240) : QColor(56, 189, 248, 220), 1.6));
            painter.setBrush(isCamSelected ? QColor(251, 191, 36, 22) : QColor(56, 189, 248, 16));
            painter.drawPolygon(QPolygonF({pTL, pTR, pBR, pBL}));

            // Direction UP indicator triangle on far plane top edge
            const QPointF pMidTop = (pTL + pTR) * 0.5;
            bool fUp = false;
            const QPointF pUpMarker = projectWorldToScreen(centerFar + up * (frustumH * 0.65f), viewProj, &fUp);
            if (fUp) {
                painter.setBrush(isCamSelected ? QColor("#fbbf24") : QColor("#38bdf8"));
                painter.setPen(Qt::NoPen);
                painter.drawPolygon(QPolygonF({pMidTop - QPointF(4, 0), pMidTop + QPointF(4, 0), pUpMarker}));
            }
        }

        // Selection rings if camera is selected
        if (isCamSelected) {
            painter.setBrush(Qt::NoBrush);
            painter.setPen(QPen(QColor("#fbbf24"), 2.0));
            painter.drawEllipse(pApex, 8.0, 8.0);
            painter.setPen(QPen(QColor(251, 191, 36, 130), 1.2, Qt::DashLine));
            painter.drawEllipse(pApex, 13.0, 13.0);
        } else {
            painter.setBrush(QColor("#38bdf8"));
            painter.setPen(QPen(QColor("#0f172a"), 1.2));
            painter.drawEllipse(pApex, 4.5, 4.5);
        }

        // Camera badge pill
        const QString badgeText = QString("🎥 ShotCam • %1mm").arg(static_cast<int>(std::round(curSample.focalLength)));
        QFont badgeFont = painter.font();
        badgeFont.setPixelSize(10);
        badgeFont.setBold(true);
        painter.setFont(badgeFont);
        const int badgeTextW = painter.fontMetrics().horizontalAdvance(badgeText);
        const QRectF badgeRect(pApex.x() - badgeTextW * 0.5 - 6, pApex.y() - 25, badgeTextW + 12, 17);
        painter.setBrush(QColor(15, 23, 42, 230));
        painter.setPen(QPen(isCamSelected ? QColor("#fbbf24") : QColor("#38bdf8"), 1.2));
        painter.drawRoundedRect(badgeRect, 8.5, 8.5);
        painter.setPen(isCamSelected ? QColor("#fbbf24") : QColor("#e2e8f0"));
        painter.drawText(badgeRect, Qt::AlignCenter, badgeText);
    }

    // 4. Look-at line & green arrow aiming target (always active by default)
    if (rig.aimMode == CameraAimMode::LookAt) {
        const QVector3D tgtPos = resolvedAimTargetPosition();
        bool fTgt = false;
        const QPointF pTgt = projectWorldToScreen(tgtPos, viewProj, &fTgt);

        if (fApex && fTgt) {
            // Direction vector and math for chevrons & green arrow
            const QPointF dir = pTgt - pApex;
            const double dist = std::hypot(dir.x(), dir.y());

            if (dist > 14.0) {
                const QPointF norm = dir / dist;
                const QPointF perp(-norm.y(), norm.x());

                // Sightline from camera to target point
                painter.setPen(QPen(QColor(34, 197, 94, 210), 2.2, Qt::DashLine));
                painter.drawLine(pApex, pTgt);

                // Direction chevrons along sightline showing gaze flow
                painter.setPen(QPen(QColor(74, 222, 128, 230), 2.0));
                for (double frac : {0.33, 0.66}) {
                    const QPointF cPt = pApex + dir * frac;
                    QPolygonF chev;
                    chev << cPt - norm * 7.0 + perp * 5.0 << cPt << cPt - norm * 7.0 - perp * 5.0;
                    painter.drawPolyline(chev);
                }

                // Prominent GREEN ARROW pointing directly to where the camera looks
                const QPointF pArrowTip = pTgt;
                const double arrowLen = std::min(28.0, dist * 0.45);
                const double arrowWidth = std::min(14.0, arrowLen * 0.55);
                const QPointF pBaseL = pArrowTip - norm * arrowLen + perp * arrowWidth;
                const QPointF pBaseR = pArrowTip - norm * arrowLen - perp * arrowWidth;
                const QPointF pNotch = pArrowTip - norm * (arrowLen * 0.70);

                QPolygonF arrowPoly;
                arrowPoly << pArrowTip << pBaseL << pNotch << pBaseR;

                const bool isAimSel = myImpl->selectedAimTarget;
                painter.setBrush(isAimSel ? QColor(74, 222, 128) : QColor(34, 197, 94));
                painter.setPen(QPen(isAimSel ? QColor("#fbbf24") : QColor(15, 23, 42), isAimSel ? 2.0 : 1.5));
                painter.drawPolygon(arrowPoly);
            }
        }
    }

    // 5. Active drawing stroke preview
    if (myImpl->cameraTools->isDrawing()) {
        const auto& stroke = myImpl->cameraTools->activeStroke();
        if (stroke.size() >= 2) {
            painter.setPen(QPen(QColor("#fbbf24"), 2.5, Qt::DashLine));
            for (int i = 0; i < stroke.size() - 1; ++i) {
                bool f1 = false, f2 = false;
                const QPointF q1 = projectWorldToScreen(stroke[i], viewProj, &f1);
                const QPointF q2 = projectWorldToScreen(stroke[i + 1], viewProj, &f2);
                if (f1 && f2) painter.drawLine(q1, q2);
            }
        }
    }

    // Scrub ghosts are overlays, so previews never modify the composed actors.
    const double time = myImpl->currentFrame / myImpl->fps;
    const auto tracks = myImpl->directorBrief["tracks"].toObject();
    const auto paths = myImpl->directorBrief["paths"].toObject();
    for (auto track = tracks.begin(); track != tracks.end(); ++track) {
        QVector3D position; bool found = false; QString action;
        for (const auto& value : track.value().toObject()["segments"].toArray()) {
            const auto seg = value.toObject(); const auto range = seg["t"].toArray();
            if (range.size() != 2 || time < range[0].toDouble()) continue;
            const auto path = paths[seg["path"].toString()].toObject(); const auto points = path["points"].toArray();
            if (points.size() < 2) continue;
            auto vector = [](const QJsonValue& v) { const auto a = v.toArray(); return QVector3D(a[0].toDouble(),a[1].toDouble(),a[2].toDouble()); };
            double length = 0; for (int i=1;i<points.size();++i) length += (vector(points[i])-vector(points[i-1])).length();
            double u = std::clamp((time-range[0].toDouble()) / std::max(.001, range[1].toDouble()-range[0].toDouble()), 0., 1.);
            if (path["speed_profile"].toString() == "ease_in_out") u = u*u*(3-2*u);
            double distance = u*length;
            position = vector(points.last());
            for (int i=1;i<points.size();++i) { const auto a=vector(points[i-1]), b=vector(points[i]); const double d=(b-a).length();
                if (distance <= d && d > 0) { position=a+(b-a)*float(distance/d); break; } distance-=d; }
            found = true; action = seg["action"].toString();
        }
        if (!found) continue;
        bool front=false, headFront=false;
        const auto foot=projectWorldToScreen(position,viewProj,&front);
        const auto head=projectWorldToScreen(position+QVector3D(0,1.7,0),viewProj,&headFront);
        if (!front || !headFront) continue;
        painter.setPen(QPen(QColor(180,220,255,170),2,Qt::DashLine));
        painter.setBrush(QColor(100,180,240,35)); const double height=std::max(12.,std::hypot(head.x()-foot.x(),head.y()-foot.y()));
        painter.drawEllipse(head,height*.08,height*.08); painter.drawLine(head,foot);
        painter.drawEllipse(foot,height*.2,height*.07);
        painter.drawText(foot+QPointF(8,-5),track.key()+" · "+action);
    }

    // 6. General Asset Movement Paths (Characters, Vehicles, Animals)
    for (auto it = myImpl->primPaths.constBegin(); it != myImpl->primPaths.constEnd(); ++it) {
        const QString primKey = it.key();
        const QVector<QVector3D>& pts = it.value();
        if (pts.size() < 2) continue;
        if (!myImpl->explicitlyDrawnPrimPaths.contains(primKey)) continue;
        const bool isBeingDrawn = myImpl->drawPathActive && myImpl->drawingPrimPath == primKey;
        if (!isBeingDrawn && primKey != myImpl->selectedPrimPath &&
            primKey != myImpl->lastDrawnPrimPath) continue;

        const QColor col = myImpl->lintPaths.contains(primKey.section('/', -1)) ? QColor("#ef4444") : primPathColor(primKey);
        const bool isSelected = (primKey == myImpl->selectedPrimPath);

        // Path lines
        if (isSelected) {
            painter.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 70), 6.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
            for (int i = 0; i < pts.size() - 1; ++i) {
                bool f1 = false, f2 = false;
                const QPointF p1 = projectWorldToScreen(pts[i], viewProj, &f1);
                const QPointF p2 = projectWorldToScreen(pts[i + 1], viewProj, &f2);
                if (f1 && f2) painter.drawLine(p1, p2);
            }
        }

        painter.setPen(QPen(col, isSelected ? 2.8 : 2.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        for (int i = 0; i < pts.size() - 1; ++i) {
            bool f1 = false, f2 = false;
            const QPointF p1 = projectWorldToScreen(pts[i], viewProj, &f1);
            const QPointF p2 = projectWorldToScreen(pts[i + 1], viewProj, &f2);
            if (f1 && f2) {
                painter.drawLine(p1, p2);

                const QPointF mid = (p1 + p2) * 0.5;
                const QPointF dir = p2 - p1;
                const double len = std::hypot(dir.x(), dir.y());
                if (len > 25.0) {
                    const double ux = dir.x() / len;
                    const double uy = dir.y() / len;
                    const double nx = -uy;
                    const double ny = ux;
                    QPolygonF arrow;
                    arrow << mid + QPointF(ux * 5, uy * 5)
                          << mid + QPointF(-ux * 5 + nx * 4, -uy * 5 + ny * 4)
                          << mid + QPointF(-ux * 2, -uy * 2)
                          << mid + QPointF(-ux * 5 - nx * 4, -uy * 5 - ny * 4);
                    painter.save();
                    painter.setBrush(col);
                    painter.setPen(Qt::NoPen);
                    painter.drawPolygon(arrow);
                    painter.restore();
                }
            }
        }

        // Joints & Badges
        for (int i = 0; i < pts.size(); ++i) {
            bool f = false;
            const QPointF pt = projectWorldToScreen(pts[i], viewProj, &f);
            if (!f) continue;

            if (i == 0) {
                painter.setPen(QPen(QColor("#0f172a"), 1.5));
                painter.setBrush(col);
                painter.drawEllipse(pt, 5.5, 5.5);

                const QString label = primKey.section('/', -1) + " [Start]";
                QFont jFont = painter.font();
                jFont.setPixelSize(10);
                jFont.setBold(isSelected);
                painter.setFont(jFont);
                const int tw = painter.fontMetrics().horizontalAdvance(label);
                const QRectF badge(pt.x() + 8, pt.y() - 11, tw + 8, 16);
                painter.setBrush(QColor(15, 23, 42, 220));
                painter.setPen(QPen(col, 1.2));
                painter.drawRoundedRect(badge, 4, 4);
                painter.setPen(QColor("#ffffff"));
                painter.drawText(badge, Qt::AlignCenter, label);
            } else if (i == pts.size() - 1) {
                painter.setPen(QPen(col, 2.0));
                painter.setBrush(QColor(15, 23, 42));
                painter.drawEllipse(pt, 5.0, 5.0);

                QFont jFont = painter.font();
                jFont.setPixelSize(9);
                painter.setFont(jFont);
                painter.setPen(col);
                painter.drawText(pt + QPointF(7, -3), QStringLiteral("[End]"));
            } else {
                painter.setPen(QPen(QColor("#0f172a"), 1.0));
                painter.setBrush(col);
                painter.drawEllipse(pt, 3.5, 3.5);
            }
        }
    }

    // 7. Active drawing stroke preview for general asset
    if (myImpl->isDrawingPrimStroke && myImpl->activePrimStroke.size() >= 2) {
        const QColor drawCol = primPathColor(myImpl->drawingPrimPath);
        painter.setPen(QPen(drawCol, 2.5, Qt::DashLine));
        for (int i = 0; i < myImpl->activePrimStroke.size() - 1; ++i) {
            bool f1 = false, f2 = false;
            const QPointF q1 = projectWorldToScreen(myImpl->activePrimStroke[i], viewProj, &f1);
            const QPointF q2 = projectWorldToScreen(myImpl->activePrimStroke[i + 1], viewProj, &f2);
            if (f1 && f2) painter.drawLine(q1, q2);
        }
    }

    painter.restore();
}

void UsdViewportWidget::drawViewfinderHud(QPainter& painter) const {
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    const CameraRigData& rig = myImpl->cameraTools->rigData();
    double targetAspect = 16.0 / 9.0;
    if (rig.aspectRatio == "2.39:1" || rig.aspectRatio == "2.39_1") targetAspect = 2.39;
    else if (rig.aspectRatio == "9:16" || rig.aspectRatio == "9_16") targetAspect = 9.0 / 16.0;
    else if (rig.aspectRatio == "1:1" || rig.aspectRatio == "1_1") targetAspect = 1.0;

    const QRectF crop = viewfinderCropRect(targetAspect);

    // 1. Draw solid cinema black pillarbox/letterbox bars
    painter.setPen(Qt::NoPen);
    painter.setBrush(QColor(10, 12, 16));
    if (crop.top() > 0) {
        painter.drawRect(QRectF(0, 0, width(), crop.top()));
        painter.drawRect(QRectF(0, crop.bottom(), width(), height() - crop.bottom()));
    }
    if (crop.left() > 0) {
        painter.drawRect(QRectF(0, 0, crop.left(), height()));
        painter.drawRect(QRectF(crop.right(), 0, width() - crop.right(), height()));
    }

    // Border around crop
    painter.setPen(QPen(QColor(70, 85, 105, 180), 1.0));
    painter.setBrush(Qt::NoBrush);
    painter.drawRect(crop);

    // 2. Safe frames (Action Safe 90%, Title Safe 80%)
    const QRectF actionSafe = crop.adjusted(crop.width() * 0.05, crop.height() * 0.05,
                                           -crop.width() * 0.05, -crop.height() * 0.05);
    const QRectF titleSafe = crop.adjusted(crop.width() * 0.10, crop.height() * 0.10,
                                          -crop.width() * 0.10, -crop.height() * 0.10);
    painter.setPen(QPen(QColor(255, 255, 255, 45), 1.0, Qt::DashLine));
    painter.drawRect(actionSafe);
    painter.setPen(QPen(QColor(255, 255, 255, 28), 1.0, Qt::DotLine));
    painter.drawRect(titleSafe);

    // 3. Rule of thirds grid & Center crosshair
    painter.setPen(QPen(QColor(255, 255, 255, 35), 0.8));
    painter.drawLine(QPointF(crop.left() + crop.width() / 3.0, crop.top()),
                     QPointF(crop.left() + crop.width() / 3.0, crop.bottom()));
    painter.drawLine(QPointF(crop.left() + crop.width() * 2.0 / 3.0, crop.top()),
                     QPointF(crop.left() + crop.width() * 2.0 / 3.0, crop.bottom()));
    painter.drawLine(QPointF(crop.left(), crop.top() + crop.height() / 3.0),
                     QPointF(crop.right(), crop.top() + crop.height() / 3.0));
    painter.drawLine(QPointF(crop.left(), crop.top() + crop.height() * 2.0 / 3.0),
                     QPointF(crop.right(), crop.top() + crop.height() * 2.0 / 3.0));

    const QPointF center = crop.center();
    painter.setPen(QPen(QColor(255, 255, 255, 90), 1.2));
    painter.drawLine(center - QPointF(12, 0), center + QPointF(12, 0));
    painter.drawLine(center - QPointF(0, 12), center + QPointF(0, 12));

    // 4. Viewfinder Top HUD Bar inside crop
    const QRectF hudTop(crop.left() + 12, crop.top() + 10, crop.width() - 24, 28);

    const QString shotSizeName = CameraRigEvaluator::shotSizeToString(rig.shotSize).toUpper().replace('_', ' ');
    const QString lensText = QString("🎥 VIEWFINDER · %1mm · %2 · %3")
                                 .arg(static_cast<int>(std::round(cameraFocalLength())))
                                 .arg(shotSizeName)
                                 .arg(rig.aspectRatio);

    QFont hudFont = painter.font();
    hudFont.setPixelSize(11);
    hudFont.setBold(true);
    painter.setFont(hudFont);
    QFontMetrics fm(hudFont);
    const int badgeW = fm.horizontalAdvance(lensText) + 20;
    const QRectF badgeRect(hudTop.left(), hudTop.top(), badgeW, 24);

    painter.setBrush(QColor(20, 25, 32, 210));
    painter.setPen(QPen(QColor(70, 85, 105), 1.0));
    painter.drawRoundedRect(badgeRect, 4, 4);

    painter.setPen(QColor("#f8fafc"));
    painter.drawText(badgeRect, Qt::AlignCenter, lensText);

    if (rig.dollyZoomEnabled) {
        const QRectF dzRect(badgeRect.right() + 8, hudTop.top(), 110, 24);
        painter.setBrush(QColor(180, 83, 9, 220));
        painter.setPen(QPen(QColor(251, 191, 36), 1.0));
        painter.drawRoundedRect(dzRect, 4, 4);
        painter.setPen(QColor("#fef3c7"));
        painter.drawText(dzRect, Qt::AlignCenter, "⚡ DOLLY ZOOM");
    }

    const double seconds = myImpl->fps > 0 ? (myImpl->currentFrame / myImpl->fps) : 0.0;
    const int minutes = static_cast<int>(seconds) / 60;
    const double secRemainder = seconds - minutes * 60;
    const QString tcText = QString("TC %1:%2:%3")
                               .arg(minutes, 2, 10, QChar('0'))
                               .arg(static_cast<int>(secRemainder), 2, 10, QChar('0'))
                               .arg(static_cast<int>((myImpl->currentFrame - std::floor(myImpl->currentFrame)) * myImpl->fps), 2, 10, QChar('0'));

    const int tcW = fm.horizontalAdvance(tcText) + 20;
    const QRectF tcRect(hudTop.right() - tcW, hudTop.top(), tcW, 24);
    painter.setBrush(QColor(20, 25, 32, 210));
    painter.setPen(QPen(QColor(70, 85, 105), 1.0));
    painter.drawRoundedRect(tcRect, 4, 4);
    painter.setPen(QColor("#f8fafc"));
    painter.drawText(tcRect, Qt::AlignCenter, tcText);

    if (myImpl->playing) {
        const QRectF recRect(tcRect.left() - 65, hudTop.top(), 58, 24);
        painter.setBrush(QColor(220, 38, 38, 230));
        painter.setPen(QPen(QColor(254, 202, 202), 1.0));
        painter.drawRoundedRect(recRect, 4, 4);
        painter.setPen(QColor("#ffffff"));
        painter.drawText(recRect, Qt::AlignCenter, "● REC");
    }

    painter.restore();
}

void UsdViewportWidget::paintEvent(QPaintEvent*) {
    const int w = std::max(64, width());
    const int h = std::max(64, height());
    const double previewScale = myImpl->playing ? kPlaybackPreviewScale : 1.0;
    const int renderW = std::max(64, static_cast<int>(std::round(w * previewScale)));
    const int renderH = std::max(64, static_cast<int>(std::round(h * previewScale)));
    if (myImpl->hydraDirty || myImpl->cachedFrame.width() != renderW || myImpl->cachedFrame.height() != renderH) {
        renderHydraToImage(renderW, renderH, myImpl->cachedFrame);
        myImpl->hydraDirty = false;
    }

    QPainter painter(this);
    painter.drawImage(rect(), myImpl->cachedFrame);

    if (myImpl->viewMode == ViewMode::CameraViewfinder) {
        drawViewfinderHud(painter);
        drawStatusBanner(painter);
    } else {
        const double aspect = static_cast<double>(w) / static_cast<double>(h);
        const QMatrix4x4 vp = currentProjectionMatrix(aspect) * currentViewMatrix();

        drawGridOverlay(painter, vp);
        drawOrbitCenterOverlay(painter, vp);
        drawCameraFrustumAndPath(painter, vp);
        drawSelectionAndGizmoOverlay(painter, vp);
        drawActorNameTags(painter, vp);
        drawOrientationGizmo(painter);
        drawStatusBanner(painter);

        // Subtle 3D World mode badge in top-left
        painter.save();
        QFont modeFont = painter.font();
        modeFont.setPixelSize(11);
        modeFont.setBold(true);
        painter.setFont(modeFont);
        const QString worldBadgeText = QStringLiteral("🌐 3D WORLD · Tab for Viewfinder");
        QFontMetrics fm(modeFont);
        const int badgeW = fm.horizontalAdvance(worldBadgeText) + 16;
        const QRectF badgeRect(12, 12, badgeW, 22);
        painter.setBrush(QColor(15, 20, 26, 200));
        painter.setPen(QPen(QColor(60, 75, 95), 1.0));
        painter.drawRoundedRect(badgeRect, 4, 4);
        painter.setPen(QColor("#94a3b8"));
        painter.drawText(badgeRect, Qt::AlignCenter, worldBadgeText);
        painter.restore();
    }
}

void UsdViewportWidget::resizeEvent(QResizeEvent* event) {
    QWidget::resizeEvent(event);
    myImpl->hydraDirty = true;
    update();
}

void UsdViewportWidget::mousePressEvent(QMouseEvent* event) {
    setFocus();
    myImpl->lastMousePos = event->position();

    if (myImpl->viewMode == ViewMode::CameraViewfinder) {
        if (event->button() == Qt::LeftButton || event->button() == Qt::RightButton) {
            myImpl->panning = false;
            myImpl->orbiting = false;
            myImpl->draggingGizmo = false;
            myImpl->cameraTools->setTool(CameraToolType::Aim);
            myImpl->cameraTools->handleMousePress(event);
            return;
        }
    }

    if (myImpl->cameraTools && myImpl->cameraTools->tool() != CameraToolType::None) {
        if (myImpl->cameraTools->handleMousePress(event)) {
            update();
            return;
        }
    }

    if (myImpl->drawPathActive && !myImpl->drawingPrimPath.isEmpty() &&
        myImpl->drawingPrimPath != myImpl->shotCamPath &&
        myImpl->drawingPrimPath != QStringLiteral("/World/ShotCam") &&
        !myImpl->drawingPrimPath.startsWith(QStringLiteral("/World/CameraRig"))) {
        if (event->button() == Qt::LeftButton) {
            QVector3D primPos = selectedPrimPosition();
            double planeH = primPos.y();
            QVector3D hitPt;
            if (screenToGroundPlane(event->position(), planeH, hitPt)) {
                pushUndoSnapshot();
                myImpl->isDrawingPrimStroke = true;
                myImpl->activePrimStroke.clear();
                myImpl->activePrimStroke.append(hitPt);
                update();
                return;
            }
            emit statusMessageChanged(QStringLiteral("Path did not start there. Drag across the ground in the 3D viewport."));
            return;
        }
    }

    if (event->button() == Qt::RightButton ||
        (event->button() == Qt::LeftButton && (event->modifiers() & Qt::AltModifier))) {
        if (event->modifiers() & Qt::ShiftModifier) {
            myImpl->panning = true;
        } else {
            myImpl->orbiting = true;
            if (myImpl->useShotCam) {
                setUseShotCamera(false);
            }
        }
        return;
    }

    if (event->button() == Qt::MiddleButton) {
        const bool ctrl = event->modifiers() & Qt::ControlModifier;
        const bool shift = event->modifiers() & Qt::ShiftModifier;
        myImpl->dollying = ctrl && shift;
        myImpl->zooming = ctrl && !shift;
        myImpl->panning = shift && !ctrl;
        myImpl->orbiting = !ctrl && !shift;
        if (myImpl->useShotCam) {
            setUseShotCamera(false);
        }
        return;
    }

    if (event->button() != Qt::LeftButton || !myImpl->stage) return;

    // Ground painting mode (Phase 6)
    if (myImpl->activeTool == PaintingTool::Brush) {
        QVector3D groundPt;
        if (screenToGroundPlane(event->position(), 0.01, groundPt)) {
            pushUndoSnapshot();
            myImpl->paintingStroke = true;
            myImpl->currentStrokePoints.clear();
            myImpl->currentStrokePoints.append(groundPt);
        }
        return;
    }
    if (myImpl->activeTool == PaintingTool::Eraser) {
        clearPaintStrokes();
        return;
    }

    // The camera aim target keeps its axis controls; assets move by direct dragging.
    const int hitAxis = hitTestGizmoAxis(event->position());
    if (hitAxis >= 0) {
        pushUndoSnapshot();
        myImpl->draggingGizmo = true;
        myImpl->activeGizmoAxis = hitAxis;
        myImpl->dragStartAimPos = resolvedAimTargetPosition();
        update();
        return;
    }

    // 2. Camera, Aim Target & path waypoint picking in World 3D view
    if (myImpl->viewMode == ViewMode::World && myImpl->cameraTools && event->button() == Qt::LeftButton) {
        const double aspect = static_cast<double>(width()) / static_cast<double>(std::max(1, height()));
        const QMatrix4x4 vp = currentProjectionMatrix(aspect) * currentViewMatrix();

        // 2a. Check Aim Target & Green Arrow picking
        const QVector3D aimPos = resolvedAimTargetPosition();
        bool aimFront = false;
        const QPointF aimSc = projectWorldToScreen(aimPos, vp, &aimFront);
        if (aimFront && (event->position() - aimSc).manhattanLength() <= 24.0) {
            myImpl->selectedAimTarget = true;
            myImpl->selectedPrimPath.clear();
            myImpl->cameraTools->setSelectedPointIndex(-1);
            QVector3D groundHit;
            if (screenToGroundPlane(event->position(), aimPos.y(), groundHit)) {
                pushUndoSnapshot();
                myImpl->draggingAimTarget = true;
                myImpl->dragStartAimPos = aimPos;
                myImpl->dragStartWorldHit = groundHit;
            }
            updateOverlayUi();
            update();
            return;
        }

        // 2b. Check camera control points first
        const auto& rigPts = myImpl->cameraTools->rigData().points;
        int hitPtIdx = -1;
        for (int i = 0; i < rigPts.size(); ++i) {
            bool ptFront = false;
            const QPointF sc = projectWorldToScreen(rigPts[i].position, vp, &ptFront);
            if (ptFront && (event->position() - sc).manhattanLength() <= 16.0) {
                hitPtIdx = i;
                break;
            }
        }
        if (hitPtIdx >= 0) {
            myImpl->selectedAimTarget = false;
            myImpl->cameraTools->setSelectedPointIndex(hitPtIdx);
            selectPrim(myImpl->shotCamPath.isEmpty() ? QStringLiteral("/World/ShotCam") : myImpl->shotCamPath);
            QVector3D groundHit;
            if (screenToGroundPlane(event->position(), rigPts[hitPtIdx].position.y(), groundHit)) {
                pushUndoSnapshot();
                myImpl->draggingObjectGround = true;
                myImpl->dragUndoCaptured = true;
                myImpl->dragStartPrimPos = rigPts[hitPtIdx].position;
                myImpl->dragStartWorldHit = groundHit;
            }
            update();
            return;
        }

        // 2c. Check camera body & position
        const QVector3D camPos = cameraEvaluatedPosition();
        bool camFront = false;
        const QPointF camSc = projectWorldToScreen(camPos, vp, &camFront);
        if (camFront) {
            const double d = (event->position() - camSc).manhattanLength();
            if (d <= 28.0) {
                myImpl->selectedAimTarget = false;
                myImpl->cameraTools->setSelectedPointIndex(-1);
                selectPrim(myImpl->shotCamPath.isEmpty() ? QStringLiteral("/World/ShotCam") : myImpl->shotCamPath);
                QVector3D groundHit;
                if (screenToGroundPlane(event->position(), camPos.y(), groundHit)) {
                    pushUndoSnapshot();
                    myImpl->draggingObjectGround = true;
                    myImpl->dragUndoCaptured = true;
                    myImpl->dragStartPrimPos = camPos;
                    myImpl->dragStartWorldHit = groundHit;
                }
                update();
                return;
            }
        }
    }

    // 3. Perform object picking: GPU picking if an active context exists; fast, crash-proof CPU ray picking otherwise
    QString hitPrimPath;
    QVector3D hitWorldPt;
    bool hit = false;
    if (canUseGpuPicking()) {
        hit = tryGpuPicking(event->position(), hitPrimPath, &hitWorldPt);
    }
    if (!hit) {
        hit = tryCpuRayPick(event->position(), hitPrimPath, &hitWorldPt);
    }

    if (hit && !hitPrimPath.isEmpty()) {
        myImpl->selectedAimTarget = false;
        selectPrim(hitPrimPath);
        QVector3D groundHit;
        if (screenToGroundPlane(event->position(), selectedPrimPosition().y(), groundHit)) {
            myImpl->draggingObjectGround = true;
            myImpl->dragUndoCaptured = false;
            myImpl->dragStartPrimPos = selectedPrimPosition();
            myImpl->dragStartWorldHit = groundHit;
            setCursor(Qt::ClosedHandCursor);
        }
        return;
    }

    myImpl->selectedAimTarget = false;
    selectPrim(QString());
}

void UsdViewportWidget::mouseMoveEvent(QMouseEvent* event) {
    const QPointF delta = event->position() - myImpl->lastMousePos;
    myImpl->lastMousePos = event->position();

    if (myImpl->viewMode == ViewMode::CameraViewfinder) {
        if (myImpl->cameraTools && myImpl->cameraTools->handleMouseMove(event)) {
            bakeCurrentCameraRig();
            update();
            return;
        }
    }

    if (myImpl->cameraTools && myImpl->cameraTools->tool() != CameraToolType::None) {
        if (myImpl->cameraTools->handleMouseMove(event)) {
            update();
            return;
        }
    }

    if (myImpl->drawPathActive && myImpl->isDrawingPrimStroke && (event->buttons() & Qt::LeftButton)) {
        QVector3D primPos = selectedPrimPosition();
        double planeH = primPos.y();
        QVector3D hitPt;
        if (screenToGroundPlane(event->position(), planeH, hitPt)) {
            if (myImpl->activePrimStroke.isEmpty() || (hitPt - myImpl->activePrimStroke.last()).length() > 0.15f) {
                myImpl->activePrimStroke.append(hitPt);
                update();
                return;
            }
        }
    }

    if (myImpl->zooming || myImpl->dollying) {
        zoomFreeCamera(delta.y() / kZoomDragPixelsPerStep,
                       QPointF(width() * 0.5, height() * 0.5), myImpl->dollying, false);
        return;
    }

    if (myImpl->orbiting) {
        myImpl->yaw -= delta.x() * 0.008;
        myImpl->pitch = std::clamp(myImpl->pitch + delta.y() * 0.008, -1.45, 1.45);
        myImpl->hydraDirty = true;
        update();
        return;
    }

    if (myImpl->panning) {
        if (myImpl->fixedOrbitCenter) {
            emit statusMessageChanged("Turn off Fixed Center to pan the editor view.");
            return;
        }
        const QMatrix4x4 view = currentViewMatrix();
        const QVector3D right(view(0, 0), view(0, 1), view(0, 2));
        const QVector3D up(view(1, 0), view(1, 1), view(1, 2));
        const float panSpeed = static_cast<float>(myImpl->distance * 0.0016);
        myImpl->target -= right * static_cast<float>(delta.x()) * panSpeed;
        myImpl->target += up * static_cast<float>(delta.y()) * panSpeed;
        myImpl->hydraDirty = true;
        update();
        return;
    }

    if (myImpl->paintingStroke && myImpl->stage) {
        QVector3D groundPt;
        if (screenToGroundPlane(event->position(), 0.01, groundPt)) {
            if (myImpl->currentStrokePoints.isEmpty() ||
                (groundPt - myImpl->currentStrokePoints.last()).length() > 0.08f) {
                myImpl->currentStrokePoints.append(groundPt);
            }
        }
        return;
    }

    if (myImpl->draggingGizmo && myImpl->selectedAimTarget && myImpl->activeGizmoAxis >= 0) {
        const int axis = myImpl->activeGizmoAxis;
        const float step = static_cast<float>((delta.x() - delta.y()) * 0.03);
        QVector3D pos = resolvedAimTargetPosition();
        if (axis == 0) pos.setX(pos.x() + step);
        if (axis == 1) pos.setY(pos.y() + step);
        if (axis == 2) pos.setZ(pos.z() + step);
        setAimTargetPosition(pos);
        return;
    }

    if (myImpl->draggingAimTarget) {
        QVector3D groundHit;
        if (screenToGroundPlane(event->position(), myImpl->dragStartAimPos.y(), groundHit)) {
            const QVector3D offset = groundHit - myImpl->dragStartWorldHit;
            setAimTargetPosition(myImpl->dragStartAimPos + offset);
        }
        return;
    }

    if (myImpl->draggingObjectGround && !myImpl->selectedPrimPath.isEmpty()) {
        QVector3D groundHit;
        if (screenToGroundPlane(event->position(), myImpl->dragStartPrimPos.y(), groundHit)) {
            const QVector3D offset = groundHit - myImpl->dragStartWorldHit;
            if (offset.lengthSquared() < 1e-6f) return;
            if (!myImpl->dragUndoCaptured) {
                pushUndoSnapshot();
                myImpl->dragUndoCaptured = true;
            }
            setSelectedPrimPosition(myImpl->dragStartPrimPos + QVector3D(offset.x(), 0.0f, offset.z()));
        }
        return;
    }
}

void UsdViewportWidget::mouseReleaseEvent(QMouseEvent* event) {
    Q_UNUSED(event);
    if (myImpl->paintingStroke && myImpl->stage && myImpl->currentStrokePoints.size() >= 2) {
        myImpl->suppressWatcherReload = true;
        if (myImpl->userEditsLayer) {
            myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
        }
        UsdGeomScope::Define(myImpl->stage, SdfPath("/World/Paint"));
        SdfPath strokePath;
        for (int i = myImpl->nextStrokeIndex; i < 10000; ++i) {
            SdfPath cand("/World/Paint/Stroke_" + std::to_string(i));
            if (!myImpl->stage->GetPrimAtPath(cand)) {
                strokePath = cand;
                myImpl->nextStrokeIndex = i + 1;
                break;
            }
        }
        if (!strokePath.IsEmpty()) {
            UsdGeomBasisCurves curves = UsdGeomBasisCurves::Define(myImpl->stage, strokePath);
            curves.CreateTypeAttr(VtValue(UsdGeomTokens->linear));
            VtArray<GfVec3f> pts;
            for (const QVector3D& p : myImpl->currentStrokePoints) {
                pts.push_back(GfVec3f(p.x(), p.y() + 0.015f, p.z()));
            }
            curves.CreatePointsAttr(VtValue(pts));
            VtArray<int> counts{static_cast<int>(pts.size())};
            curves.CreateCurveVertexCountsAttr(VtValue(counts));
            VtArray<float> widths{static_cast<float>(std::max(0.03, myImpl->brushSize * 0.012))};
            curves.CreateWidthsAttr(VtValue(widths));
            curves.SetWidthsInterpolation(UsdGeomTokens->constant);

            VtArray<GfVec3f> col{GfVec3f(myImpl->brushColor.redF(), myImpl->brushColor.greenF(), myImpl->brushColor.blueF())};
            UsdGeomPrimvarsAPI(curves).CreatePrimvar(
                UsdGeomTokens->primvarsDisplayColor, SdfValueTypeNames->Color3fArray, UsdGeomTokens->constant).Set(col);
        }
        if (myImpl->userEditsLayer) {
            myImpl->userEditsLayer->Save();
        }
        myImpl->suppressWatcherReload = false;
        myImpl->invalidateBound(QString());
        myImpl->hydraDirty = true;
        updateOverlayUi();
        update();
        emit stageChanged();
        emit userEditsChanged(userEditedPrimCount());
    }

    if (myImpl->draggingAimTarget || (myImpl->draggingGizmo && myImpl->selectedAimTarget)) {
        myImpl->draggingAimTarget = false;
        myImpl->draggingGizmo = false;
        myImpl->activeGizmoAxis = -1;
        bakeCurrentCameraRig();
        if (myImpl->stage && myImpl->userEditsLayer && myImpl->cameraTools) {
            myImpl->suppressWatcherReload = true;
            myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
            saveCameraRigToStage(myImpl->stage, myImpl->cameraTools->rigData(), myImpl->startFrame, myImpl->endFrame, myImpl->fps);
            myImpl->userEditsLayer->Save();
            myImpl->suppressWatcherReload = false;
        }
        updateOverlayUi();
        update();
        emit stageChanged();
        emit userEditsChanged(userEditedPrimCount());
        return;
    }

    if (myImpl->cameraTools && myImpl->cameraTools->handleMouseRelease(event)) {
        setCurrentFrame(myImpl->startFrame);
        bakeCurrentCameraRig();
        stopDrawingPath();
        update();
        return;
    }

    if (myImpl->drawPathActive && myImpl->isDrawingPrimStroke) {
        myImpl->isDrawingPrimStroke = false;
        if (myImpl->activePrimStroke.size() >= 2) {
            QVector<QVector3D> simplified = simplifyPointsRdp(myImpl->activePrimStroke, 0.1);
            const QString completedPath = myImpl->drawingPrimPath;
            myImpl->primPaths[completedPath] = simplified;
            myImpl->explicitlyDrawnPrimPaths.insert(completedPath);
            myImpl->lastDrawnPrimPath = completedPath;
            persistPrimPaths();
            myImpl->activePrimStroke.clear();
            stopDrawingPath();
            emit primPathChanged(completedPath);
            emit statusMessageChanged(QString("Sketched %1 points for %2 movement path")
                                          .arg(simplified.size())
                                          .arg(completedPath));
            update();
            return;
        }
        myImpl->activePrimStroke.clear();
        stopDrawingPath();
        update();
    }

    myImpl->orbiting = false;
    myImpl->panning = false;
    myImpl->zooming = false;
    myImpl->dollying = false;
    myImpl->draggingGizmo = false;
    myImpl->draggingObjectGround = false;
    myImpl->dragUndoCaptured = false;
    myImpl->paintingStroke = false;
    myImpl->activeGizmoAxis = -1;
    unsetCursor();

    if (myImpl->pendingDragSave) {
        myImpl->pendingDragSave = false;
        myImpl->suppressWatcherReload = true;
        if (myImpl->userEditsLayer) {
            myImpl->userEditsLayer->Save();
        }
        myImpl->suppressWatcherReload = false;
        updateOverlayUi();
        emit stageChanged();
        emit userEditsChanged(userEditedPrimCount());
    }

    update();
}

void UsdViewportWidget::zoomFreeCamera(double steps, const QPointF& screenPos,
                                       bool dolly, bool zoomToMouse) {
    if (std::abs(steps) < 1e-4) return;
    if (myImpl->useShotCam) setUseShotCamera(false);
    if (myImpl->fixedOrbitCenter) {
        dolly = false;
        zoomToMouse = false;
    }

    // Dolly translates the eye and orbit center together, so it can pass through
    // the orbit center. Ordinary zoom changes their separation instead.
    if (dolly) {
        const QMatrix4x4 inverseView = currentViewMatrix().inverted();
        const QVector3D forward = -inverseView.mapVector(QVector3D(0.0f, 0.0f, 1.0f)).normalized();
        const double travel = myImpl->distance * (1.0 - std::pow(kZoomFactorPerStep, steps));
        myImpl->target += forward * static_cast<float>(travel);
        myImpl->hydraDirty = true;
        update();
        return;
    }

    QVector3D anchor = myImpl->target;
    QString hitPrimPath;
    if (zoomToMouse && !tryCpuRayPick(screenPos, hitPrimPath, &anchor) &&
        !screenToGroundPlane(screenPos, 0.0, anchor)) {
        QVector3D origin, direction;
        if (screenToRay(screenPos, origin, direction)) {
            const QMatrix4x4 inverseView = currentViewMatrix().inverted();
            const QVector3D forward = -inverseView.mapVector(QVector3D(0.0f, 0.0f, 1.0f)).normalized();
            const float denominator = QVector3D::dotProduct(direction, forward);
            if (std::abs(denominator) > 1e-4f) {
                const float depth = QVector3D::dotProduct(myImpl->target - origin, forward) / denominator;
                anchor = origin + direction * std::max(0.0f, depth);
            } else {
                anchor = myImpl->target;
            }
        } else {
            anchor = myImpl->target;
        }
    }

    const double oldDistance = myImpl->distance;
    const double newDistance = std::clamp(oldDistance * std::pow(kZoomFactorPerStep, steps),
                                          kFreeViewNearClip * 2.0, kFreeViewFarClip * 0.5);
    const float scale = static_cast<float>(newDistance / oldDistance);
    myImpl->target = anchor + (myImpl->target - anchor) * scale;
    myImpl->distance = newDistance;
    myImpl->hydraDirty = true;
    update();
}

void UsdViewportWidget::wheelEvent(QWheelEvent* event) {
    if (myImpl->viewMode == ViewMode::CameraViewfinder) {
        if (event->modifiers() & Qt::ShiftModifier) {
            const double delta = event->angleDelta().y() > 0 ? 0.35 : -0.35;
            auto& rig = myImpl->cameraTools->rigData();
            if (!rig.points.isEmpty()) {
                CameraRigEvaluator eval(myImpl->fps);
                CameraSample cur = eval.evaluate(rig, myImpl->currentFrame / myImpl->fps);
                QMatrix4x4 camMat;
                camMat.rotate(cur.rotationDeg.y(), 0.0f, 1.0f, 0.0f);
                camMat.rotate(cur.rotationDeg.x(), 1.0f, 0.0f, 0.0f);
                const QVector3D fwd = camMat.mapVector(QVector3D(0.0f, 0.0f, -1.0f));
                for (auto& pt : rig.points) {
                    pt.position += fwd * static_cast<float>(delta);
                }
                bakeCurrentCameraRig();
            }
        } else {
            const double delta = event->angleDelta().y() > 0 ? 2.0 : -2.0;
            setCameraFocalLength(cameraFocalLength() + delta);
        }
        return;
    }

    if (myImpl->selectedAimTarget && (event->modifiers() & Qt::ShiftModifier)) {
        const double delta = event->angleDelta().y() > 0 ? 0.2 : -0.2;
        QVector3D pos = resolvedAimTargetPosition();
        pos.setY(std::max(0.0f, pos.y() + static_cast<float>(delta)));
        setAimTargetPosition(pos);
        bakeCurrentCameraRig();
        return;
    }

    if (myImpl->cameraTools && myImpl->cameraTools->handleWheel(event)) {
        update();
        return;
    }

    const double steps = !event->angleDelta().isNull()
        ? event->angleDelta().y() / 120.0
        : event->pixelDelta().y() / 100.0;
    zoomFreeCamera(steps, event->position());
    event->accept();
}

void UsdViewportWidget::keyPressEvent(QKeyEvent* event) {
    if (event->key() == Qt::Key_C && (event->modifiers() & (Qt::MetaModifier | Qt::ControlModifier)) && (event->modifiers() & Qt::ShiftModifier)) {
        snapCameraToView(); event->accept(); return;
    }
    if (event->key() == Qt::Key_Tab) {
        toggleViewMode();
        event->accept();
        return;
    }
    if (event->key() == Qt::Key_C && !(event->modifiers() & (Qt::ControlModifier | Qt::MetaModifier))) {
        selectPrim(myImpl->shotCamPath);
        event->accept();
        return;
    }
    if (myImpl->cameraTools && myImpl->cameraTools->handleKeyPress(event)) {
        event->accept();
        return;
    }

    if (myImpl->viewMode == ViewMode::World && (event->modifiers() & Qt::KeypadModifier) &&
        (event->key() == Qt::Key_Plus || event->key() == Qt::Key_Minus)) {
        zoomFreeCamera(event->key() == Qt::Key_Plus ? 1.0 : -1.0,
                       QPointF(width() * 0.5, height() * 0.5), false, false);
        event->accept();
        return;
    }

    if (event->matches(QKeySequence::Undo)) {
        undo();
        return;
    }
    if (event->matches(QKeySequence::Redo)) {
        redo();
        return;
    }
    switch (event->key()) {
        case Qt::Key_Space:
            setPlaying(!myImpl->playing);
            break;
        case Qt::Key_F:
            frameSelectedPrim();
            break;
        case Qt::Key_Delete:
        case Qt::Key_Backspace:
            if (myImpl->cameraTools && myImpl->cameraTools->selectedPointIndex() >= 0) {
                deleteSelectedCameraPathJoint();
            } else {
                deleteSelectedPrim();
            }
            break;
        case Qt::Key_D:
            if (event->modifiers() & Qt::ControlModifier) {
                duplicateSelectedPrim();
            }
            break;
        default:
            QWidget::keyPressEvent(event);
            break;
    }
}


QJsonObject UsdViewportWidget::cameraDocument() const { return myImpl->cameraDocument; }

void UsdViewportWidget::setCameraDocument(const QJsonObject& document) {
    if (!myImpl->cameraTools || document["version"].toInt() != 1) return;
    const QString cid = document["active_camera"].toString();
    if (!QRegularExpression("^[A-Za-z_][A-Za-z0-9_]*$").match(cid).hasMatch()) return;
    const auto camera = document["cameras"].toObject()[cid].toObject();
    if (camera.isEmpty()) return;
    const bool suppress = myImpl->suppressWatcherReload;
    myImpl->suppressWatcherReload = true;
    myImpl->cameraDocument = document;
    CameraRigData rig = cameraRigFromJson(camera);
    for (auto& target : rig.aimTargets) {
        for (const auto& item : primList()) if (item.actorId == target) { target = item.path; break; }
    }
    myImpl->cameraTools->setRigData(rig);
    const QString file = QFileInfo(myImpl->stagePath).dir().filePath("cam_" + cid + ".usda");
    if (myImpl->userEditsLayer && QFileInfo::exists(file)) {
        const auto paths = myImpl->userEditsLayer->GetSubLayerPaths();
        if (std::find(paths.begin(), paths.end(), file.toStdString()) == paths.end())
            myImpl->userEditsLayer->GetSubLayerPaths().push_back(file.toStdString());
        myImpl->shotCamPath = "/World/Cameras/" + cid;
    }
    myImpl->suppressWatcherReload = suppress;
}

void UsdViewportWidget::snapCameraToView() {
    if (!myImpl->stage || !myImpl->cameraTools) return;
    pushUndoSnapshot();
    auto rig = myImpl->cameraTools->rigData();
    const double cp = std::cos(myImpl->pitch);
    CameraRigPoint point;
    point.position = myImpl->target + QVector3D(std::sin(myImpl->yaw)*cp, std::sin(myImpl->pitch), std::cos(myImpl->yaw)*cp)*myImpl->distance;
    point.focalLength = rig.focalStart;
    if (rig.points.isEmpty()) rig.pathStartS = myImpl->currentFrame/myImpl->fps;
    rig.points.append(point);
    myImpl->cameraTools->setRigData(rig);
    bakeCurrentCameraRig();
    emit statusMessageChanged("View added as a camera waypoint");
}

void UsdViewportWidget::persistPrimPaths() {
    if (!myImpl->userEditsLayer) return;
    QJsonObject paths;
    for (auto it = myImpl->primPaths.begin(); it != myImpl->primPaths.end(); ++it) {
        QJsonArray points;
        for (const auto& p : it.value()) points.append(QJsonArray{p.x(), p.y(), p.z()});
        paths[it.key()] = points;
    }
    auto metadata = myImpl->userEditsLayer->GetCustomLayerData();
    metadata["director_actor_paths"] = VtValue(QJsonDocument(paths).toJson(QJsonDocument::Compact).toStdString());
    QJsonArray drawnPaths;
    for (const auto& path : myImpl->explicitlyDrawnPrimPaths) drawnPaths.append(path);
    metadata["director_drawn_paths"] = VtValue(QJsonDocument(drawnPaths).toJson(QJsonDocument::Compact).toStdString());
    myImpl->userEditsLayer->SetCustomLayerData(metadata);
    myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    for (auto it = myImpl->primPaths.begin(); it != myImpl->primPaths.end(); ++it) {
        if (!it.key().startsWith("/World/Paths/") || it.value().size() < 2 ||
            !myImpl->explicitlyDrawnPrimPaths.contains(it.key())) continue;
        auto curve = UsdGeomBasisCurves::Define(myImpl->stage, SdfPath(it.key().toStdString()));
        curve.CreatePurposeAttr().Set(UsdGeomTokens->guide);
        curve.CreateTypeAttr().Set(UsdGeomTokens->linear);
        VtVec3fArray points; for (const auto& p : it.value()) points.push_back(GfVec3f(p.x(),p.y(),p.z()));
        curve.CreatePointsAttr().Set(points); curve.CreateCurveVertexCountsAttr().Set(VtIntArray{int(points.size())});
    }
    myImpl->userEditsLayer->Save();
}
void UsdViewportWidget::setDirectorBrief(const QJsonObject& brief) { myImpl->directorBrief = brief; update(); }
void UsdViewportWidget::setPathLint(const QStringList& paths) { myImpl->lintPaths = paths; update(); }

QString UsdViewportWidget::captureUndoState() const {
    if (!myImpl->userEditsLayer) return {};
    std::string layer; myImpl->userEditsLayer->ExportToString(&layer);
    QFile authored(myImpl->userEditsPath); QByteArray raw;
    if (authored.open(QIODevice::ReadOnly)) raw = authored.readAll();
    const QString file = QFileInfo(myImpl->stagePath).dir().filePath("camera_rig.json");
    QFile camera(file); const bool exists = camera.exists(); QByteArray cameraBytes;
    if (camera.open(QIODevice::ReadOnly)) cameraBytes = camera.readAll();
    QJsonObject paths;
    for (auto it=myImpl->primPaths.begin();it!=myImpl->primPaths.end();++it) {
        QJsonArray points; for(const auto& p:it.value())points.append(QJsonArray{p.x(),p.y(),p.z()});paths[it.key()]=points;
    }
    return QString::fromUtf8(QJsonDocument(QJsonObject{{"layer",QString::fromStdString(layer)}, {"raw_layer",QString::fromLatin1(raw.toBase64())},
        {"camera_exists",exists},{"camera",QString::fromLatin1(cameraBytes.toBase64())},{"paths",paths},
        {"drawn_paths",QJsonArray::fromStringList(explicitlyDrawnPrimPaths())}}).toJson(QJsonDocument::Compact));
}

void UsdViewportWidget::restoreUndoState(const QString& snapshot) {
    const auto state=QJsonDocument::fromJson(snapshot.toUtf8()).object();
    myImpl->userEditsLayer->ImportFromString(state["layer"].toString().toStdString());
    QSaveFile layer(myImpl->userEditsPath);
    if(layer.open(QIODevice::WriteOnly)){layer.write(QByteArray::fromBase64(state["raw_layer"].toString().toLatin1()));layer.commit();}
    myImpl->primPaths.clear();const auto paths=state["paths"].toObject();
    for(auto it=paths.begin();it!=paths.end();++it){QVector<QVector3D> points;for(const auto& value:it.value().toArray()){auto a=value.toArray();if(a.size()==3)points.append(QVector3D(a[0].toDouble(),a[1].toDouble(),a[2].toDouble()));}myImpl->primPaths[it.key()]=points;}
    QStringList drawnPaths;
    for (const auto& value : state["drawn_paths"].toArray()) drawnPaths.append(value.toString());
    setExplicitlyDrawnPrimPaths(drawnPaths);
    myImpl->lastDrawnPrimPath.clear();
    const QString file=QFileInfo(myImpl->stagePath).dir().filePath("camera_rig.json");
    if(state["camera_exists"].toBool()){
        const auto bytes=QByteArray::fromBase64(state["camera"].toString().toLatin1());QSaveFile camera(file);
        if(camera.open(QIODevice::WriteOnly)){camera.write(bytes);camera.commit();}
        setCameraDocument(QJsonDocument::fromJson(bytes).object());myImpl->cameraPreviewTimer->start();
    }else{
        QFile::remove(file);myImpl->cameraDocument={};myImpl->shotCamPath="/World/ShotCam";
        CameraRigData rig;if(convertShotCamToRig(myImpl->stage,myImpl->startFrame,myImpl->endFrame,myImpl->fps,rig))myImpl->cameraTools->setRigData(rig);
    }
    emit primPathChanged(QString());
}

void UsdViewportWidget::flushCameraPreview() {
    if (myImpl->cameraPreviewTimer) myImpl->cameraPreviewTimer->stop();
    if (myImpl->cameraPreviewProcess) {
        auto* process=myImpl->cameraPreviewProcess;
        if (!process->waitForFinished(5000)) {process->kill();process->waitForFinished(1000);}
    }
}
