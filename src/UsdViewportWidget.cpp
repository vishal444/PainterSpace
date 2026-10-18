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
#include "pxr/usd/usdGeom/bboxCache.h"
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
#include <QPushButton>
#include <QResizeEvent>
#include <QSaveFile>
#include <QSignalBlocker>
#include <QSlider>
#include <QTimer>
#include <QVBoxLayout>
#include <QOpenGLContext>
#include <QWheelEvent>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <memory>

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
    bool useShotCam = true;
    QString shotCamPath = QStringLiteral("/World/ShotCam");
    double yaw = 0.45;
    double pitch = 0.32;
    double distance = 12.0;
    QVector3D target = QVector3D(0.0f, 1.6f, -2.0f);

    // Interaction state
    PaintingTool activeTool = PaintingTool::Select;
    UsdGizmoMode gizmoMode = UsdGizmoMode::Move;
    QString selectedPrimPath;
    bool orbiting = false;
    bool panning = false;
    bool draggingGizmo = false;
    bool draggingObjectGround = false;
    bool paintingStroke = false;
    int activeGizmoAxis = -1; // 0=X, 1=Y, 2=Z
    QPointF lastMousePos;
    QVector3D dragStartPrimPos;
    QVector3D dragStartPrimRot;
    double dragStartPrimScale = 1.0;
    QVector3D dragStartWorldHit;
    QVector<QVector3D> currentStrokePoints;
    int nextStrokeIndex = 1;

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
    QComboBox* gizmoCombo = nullptr;
    QLabel* userEditsBadge = nullptr;
    QCheckBox* userEditsCheck = nullptr;
    QPushButton* resetEditsButton = nullptr;
};

UsdViewportWidget::UsdViewportWidget(QWidget* parent)
    : QWidget(parent), myImpl(std::make_unique<Impl>()) {
    ensureSystemUsdInitialized();
    setFocusPolicy(Qt::StrongFocus);
    setMouseTracking(true);
    setMinimumSize(480, 320);

    myImpl->playTimer = new QTimer(this);
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
        "QWidget#usdBottomBar { background: rgba(21, 25, 30, 225); border: 1px solid #343e4b; border-radius: 8px; }"
        "QPushButton { background: #242c35; color: #e3e8ed; border: 1px solid #3c4856; border-radius: 5px; padding: 4px 10px; font-size: 11px; font-weight: 600; }"
        "QPushButton:hover { background: #313d4b; border-color: #73b5da; }"
        "QComboBox { background: #20262e; color: #e3e8ed; border: 1px solid #3c4856; border-radius: 5px; padding: 3px 8px; font-size: 11px; min-height: 22px; }"
        "QLabel { color: #cfd8e2; font-size: 11px; }"
        "QCheckBox { color: #cfd8e2; font-size: 11px; spacing: 5px; }");

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

    myImpl->cameraCombo = new QComboBox(myImpl->bottomBar);
    myImpl->cameraCombo->addItem("Camera: ShotCam", true);
    myImpl->cameraCombo->addItem("Camera: Free Orbit", false);
    connect(myImpl->cameraCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int idx) {
        setUseShotCamera(myImpl->cameraCombo->itemData(idx).toBool());
    });
    barLayout->addWidget(myImpl->cameraCombo);

    myImpl->gizmoCombo = new QComboBox(myImpl->bottomBar);
    myImpl->gizmoCombo->addItem("Gizmo: Move (W)", static_cast<int>(UsdGizmoMode::Move));
    myImpl->gizmoCombo->addItem("Gizmo: Rotate (E)", static_cast<int>(UsdGizmoMode::Rotate));
    myImpl->gizmoCombo->addItem("Gizmo: Scale (R)", static_cast<int>(UsdGizmoMode::Scale));
    connect(myImpl->gizmoCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, [this](int idx) {
        setGizmoMode(static_cast<UsdGizmoMode>(myImpl->gizmoCombo->itemData(idx).toInt()));
    });
    barLayout->addWidget(myImpl->gizmoCombo);

    myImpl->userEditsBadge = new QLabel("Your changes: 0", myImpl->bottomBar);
    myImpl->userEditsBadge->setStyleSheet(
        "background: #273849; color: #8ad0f5; border: 1px solid #416382; border-radius: 4px; padding: 3px 7px; font-weight: 700;");
    barLayout->addWidget(myImpl->userEditsBadge);

    myImpl->userEditsCheck = new QCheckBox("Apply", myImpl->bottomBar);
    myImpl->userEditsCheck->setChecked(true);
    myImpl->userEditsCheck->setToolTip("Toggle applying the separate user_edits.usda layer over shot.usda");
    connect(myImpl->userEditsCheck, &QCheckBox::toggled, this, &UsdViewportWidget::setUserEditsEnabled);
    barLayout->addWidget(myImpl->userEditsCheck);

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
    {
        QSignalBlocker blockGizmo(myImpl->gizmoCombo);
        myImpl->gizmoCombo->setCurrentIndex(static_cast<int>(myImpl->gizmoMode));
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

    if (myImpl->userEditsLayer) {
        if (!myImpl->userEditsEnabled) {
            myImpl->stage->MuteLayer(myImpl->userEditsLayer->GetIdentifier());
        }
        myImpl->stage->SetEditTarget(UsdEditTarget(myImpl->userEditsLayer));
    }

    // Recreate engine for the newly opened stage
    myImpl->engine.reset();

    // Read stage timing metadata
    const double startTc = myImpl->stage->HasAuthoredTimeCodeRange() ? myImpl->stage->GetStartTimeCode() : 0.0;
    const double endTc = myImpl->stage->HasAuthoredTimeCodeRange() ? myImpl->stage->GetEndTimeCode() : 120.0;
    const double tcps = myImpl->stage->GetTimeCodesPerSecond();
    myImpl->startFrame = startTc;
    myImpl->endFrame = std::max(startTc, endTc);
    myImpl->fps = tcps > 0.0 ? tcps : 24.0;
    myImpl->currentFrame = std::clamp(myImpl->currentFrame, myImpl->startFrame, myImpl->endFrame);
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
    myImpl->useShotCam = shotCamPrim && shotCamPrim.IsA<UsdGeomCamera>() && !isSceneStage;

    // Seed undo stack with initial user_edits.usda content
    myImpl->undoStack.clear();
    myImpl->redoStack.clear();
    if (myImpl->userEditsLayer) {
        std::string exported;
        myImpl->userEditsLayer->ExportToString(&exported);
        myImpl->undoStack.append(QString::fromStdString(exported));
    }

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
    if (!toWatch.isEmpty()) {
        myImpl->fileWatcher->addPaths(toWatch);
    }
}

QString UsdViewportWidget::stagePath() const { return myImpl->stagePath; }
QString UsdViewportWidget::userEditsLayerPath() const { return myImpl->userEditsPath; }
bool UsdViewportWidget::hasStage() const { return static_cast<bool>(myImpl->stage); }
bool UsdViewportWidget::isSceneStage() const { return myImpl->isSceneStage; }

void UsdViewportWidget::setPlaying(bool playing) {
    if (myImpl->playing == playing) return;
    myImpl->playing = playing;
    if (playing) {
        myImpl->playTimer->start(std::max(1, static_cast<int>(std::round(1000.0 / myImpl->fps))));
    } else {
        myImpl->playTimer->stop();
    }
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

void UsdViewportWidget::setUseShotCamera(bool useShotCam) {
    if (myImpl->useShotCam == useShotCam) return;
    myImpl->useShotCam = useShotCam;
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
}

bool UsdViewportWidget::useShotCamera() const { return myImpl->useShotCam; }
QString UsdViewportWidget::activeCameraPath() const {
    return myImpl->useShotCam ? myImpl->shotCamPath : QString();
}

void UsdViewportWidget::resetFreeCamera() {
    myImpl->useShotCam = false;
    myImpl->yaw = 0.45;
    myImpl->pitch = 0.32;
    myImpl->distance = 12.0;
    myImpl->target = QVector3D(0.0f, 1.6f, -2.0f);
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
}

void UsdViewportWidget::frameSelectedPrim() {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim) return;
    GfBBox3d bbox = myImpl->cachedWorldBound(prim);
    GfRange3d range = bbox.ComputeAlignedRange();
    if (!range.IsEmpty()) {
        GfVec3d c = range.GetMidpoint();
        GfVec3d sz = range.GetSize();
        myImpl->target = QVector3D(float(c[0]), float(c[1]), float(c[2]));
        myImpl->distance = std::clamp(sz.GetLength() * 1.4, 2.5, 80.0);
        myImpl->useShotCam = false;
        myImpl->hydraDirty = true;
        updateOverlayUi();
        update();
    }
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
    myImpl->selectedPrimPath = primPath;
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
    UsdPrim prim = myImpl->stage->GetPrimAtPath(SdfPath(myImpl->selectedPrimPath.toStdString()));
    if (!prim || !prim.IsA<UsdGeomXformable>()) return QVector3D();
    UsdGeomXformable xf(prim);
    GfMatrix4d localXf(1.0);
    bool resets = false;
    xf.GetLocalTransformation(&localXf, &resets, UsdTimeCode(myImpl->currentFrame));
    GfVec3d t = localXf.ExtractTranslation();
    return QVector3D(float(t[0]), float(t[1]), float(t[2]));
}

void UsdViewportWidget::setSelectedPrimPosition(const QVector3D& position) {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;
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

    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingObjectGround);
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

    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingObjectGround);
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

    const bool interactiveDrag = (myImpl->draggingGizmo || myImpl->draggingObjectGround);
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
    std::string exported;
    myImpl->userEditsLayer->ExportToString(&exported);
    const QString text = QString::fromStdString(exported);
    if (myImpl->undoStack.isEmpty() || myImpl->undoStack.last() != text) {
        myImpl->undoStack.append(text);
        if (myImpl->undoStack.size() > 50) myImpl->undoStack.removeFirst();
    }
    myImpl->redoStack.clear();
}

bool UsdViewportWidget::canUndo() const { return myImpl->undoStack.size() > 1; }
bool UsdViewportWidget::canRedo() const { return !myImpl->redoStack.isEmpty(); }

void UsdViewportWidget::undo() {
    if (!myImpl->userEditsLayer || myImpl->undoStack.isEmpty()) return;
    std::string currentStr;
    myImpl->userEditsLayer->ExportToString(&currentStr);
    const QString currentText = QString::fromStdString(currentStr);

    QString targetText = myImpl->undoStack.takeLast();
    if (targetText == currentText && !myImpl->undoStack.isEmpty()) {
        myImpl->redoStack.append(currentText);
        targetText = myImpl->undoStack.last();
    } else {
        myImpl->redoStack.append(currentText);
    }

    myImpl->suppressWatcherReload = true;
    myImpl->userEditsLayer->ImportFromString(targetText.toStdString());
    myImpl->userEditsLayer->Save();
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::redo() {
    if (!myImpl->userEditsLayer || myImpl->redoStack.isEmpty()) return;
    const QString nextText = myImpl->redoStack.takeLast();
    myImpl->undoStack.append(nextText);
    myImpl->suppressWatcherReload = true;
    myImpl->userEditsLayer->ImportFromString(nextText.toStdString());
    myImpl->userEditsLayer->Save();
    myImpl->suppressWatcherReload = false;
    myImpl->invalidateBound(QString());
    myImpl->hydraDirty = true;
    updateOverlayUi();
    update();
    emit stageChanged();
    emit userEditsChanged(userEditedPrimCount());
}

void UsdViewportWidget::setTool(PaintingTool tool) {
    myImpl->activeTool = tool;
    update();
}

PaintingTool UsdViewportWidget::tool() const { return myImpl->activeTool; }

void UsdViewportWidget::setGizmoMode(UsdGizmoMode mode) {
    if (myImpl->gizmoMode == mode) return;
    myImpl->gizmoMode = mode;
    updateOverlayUi();
    update();
}

UsdGizmoMode UsdViewportWidget::gizmoMode() const { return myImpl->gizmoMode; }
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
    proj.perspective(45.0f, safeAspect, 0.1f, 1000.0f);
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
    SdfPath bestPrimPath;

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
                const GfBBox3d bbox = myImpl->cachedWorldBound(stroke);
                if (bbox.ComputeAlignedRange().IsEmpty()) continue;
                double dMin = 0.0, dMax = 0.0;
                if (ray.Intersect(bbox, &dMin, &dMax)) {
                    const double hitDist = (dMin >= 0.0) ? dMin : ((dMax >= 0.0) ? dMax : -1.0);
                    if (hitDist >= 0.0 && hitDist < closestDist) {
                        closestDist = hitDist;
                        bestPrimPath = stroke.GetPath();
                    }
                }
            }
            continue;
        }

        const GfBBox3d bbox = myImpl->cachedWorldBound(child);
        const GfRange3d range = bbox.ComputeAlignedRange();
        if (range.IsEmpty()) continue;

        double dMin = 0.0, dMax = 0.0;
        if (ray.Intersect(bbox, &dMin, &dMax)) {
            const double hitDist = (dMin >= 0.0) ? dMin : ((dMax >= 0.0) ? dMax : -1.0);
            if (hitDist >= 0.0 && hitDist < closestDist) {
                closestDist = hitDist;
                bestPrimPath = child.GetPath();
            }
        }
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

void UsdViewportWidget::drawSelectionAndGizmoOverlay(QPainter& painter, const QMatrix4x4& viewProj) const {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return;
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

    // 2. Draw interactive 3D Move/Rotate/Scale gizmo at prim pivot
    const QVector3D origin = selectedPrimPosition();
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
        painter.setBrush(QColor("#f0c47a"));
        painter.setPen(QPen(QColor("#1b2027"), 1.5));
        painter.drawEllipse(originPt, 5.0, 5.0);
    }

    painter.restore();
}

int UsdViewportWidget::hitTestGizmoAxis(const QPointF& screenPt) const {
    if (!myImpl->stage || myImpl->selectedPrimPath.isEmpty()) return -1;
    const double aspect = static_cast<double>(width()) / static_cast<double>(std::max(1, height()));
    const QMatrix4x4 vp = currentProjectionMatrix(aspect) * currentViewMatrix();
    const QVector3D origin = selectedPrimPosition();
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

void UsdViewportWidget::drawOrientationGizmo(QPainter& painter) const {
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);

    const QRectF gizmoRect(width() - 94.0, 12.0, 82.0, 82.0);
    painter.setBrush(QColor(21, 25, 30, 200));
    painter.setPen(QPen(QColor(52, 62, 75), 1.0));
    painter.drawRoundedRect(gizmoRect, 10.0, 10.0);

    // Top badge showing OpenUSD + Hydra Storm Metal status
    const QRectF hydraBadge(12.0, 12.0, 290.0, 26.0);
    painter.setBrush(QColor(21, 25, 30, 210));
    painter.setPen(QPen(QColor(65, 99, 130), 1.0));
    painter.drawRoundedRect(hydraBadge, 6.0, 6.0);
    painter.setPen(QColor("#8ad0f5"));
    QFont f = painter.font();
    f.setPixelSize(11);
    f.setBold(true);
    painter.setFont(f);
    const QString camMode = myImpl->useShotCam ? QStringLiteral("ShotCam") : QStringLiteral("Free Orbit");
    painter.drawText(hydraBadge.adjusted(10, 0, -10, 0), Qt::AlignVCenter | Qt::AlignLeft,
                     QString("OpenUSD Hydra Storm (Metal) • %1 • %2 ms")
                         .arg(camMode)
                         .arg(myImpl->lastRenderMs, 0, 'f', 1));

    const QPointF center = gizmoRect.center();
    const QMatrix4x4 view = currentViewMatrix();
    const QVector3D axes[3] = {
        QVector3D(1.0f, 0.0f, 0.0f),
        QVector3D(0.0f, 1.0f, 0.0f),
        QVector3D(0.0f, 0.0f, 1.0f)
    };
    const QColor colors[3] = {QColor("#e85d4a"), QColor("#5ec27e"), QColor("#5b9be6")};
    const char* labels[3] = {"X", "Y", "Z"};
    for (int i = 0; i < 3; ++i) {
        const QVector3D dir = view.mapVector(axes[i]);
        const QPointF tip = center + QPointF(dir.x() * 26.0, -dir.y() * 26.0);
        painter.setPen(QPen(colors[i], 2.2));
        painter.drawLine(center, tip);
        painter.setBrush(colors[i]);
        painter.drawEllipse(tip, 4.0, 4.0);
        painter.drawText(tip + QPointF(4.0, -3.0), QString::fromLatin1(labels[i]));
    }
    painter.restore();
}

void UsdViewportWidget::drawStatusBanner(QPainter& painter) const {
    if (myImpl->statusBannerText.isEmpty()) return;
    painter.save();
    painter.setRenderHint(QPainter::Antialiasing, true);
    const QRectF bannerRect(12.0, 44.0, std::min(560.0, width() - 120.0), 30.0);
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

void UsdViewportWidget::paintEvent(QPaintEvent*) {
    const int w = std::max(64, width());
    const int h = std::max(64, height());
    if (myImpl->hydraDirty || myImpl->cachedFrame.width() != w || myImpl->cachedFrame.height() != h) {
        renderHydraToImage(w, h, myImpl->cachedFrame);
        myImpl->hydraDirty = false;
    }

    QPainter painter(this);
    painter.drawImage(rect(), myImpl->cachedFrame);

    const double aspect = static_cast<double>(w) / static_cast<double>(h);
    const QMatrix4x4 vp = currentProjectionMatrix(aspect) * currentViewMatrix();

    drawGridOverlay(painter, vp);
    drawSelectionAndGizmoOverlay(painter, vp);
    drawOrientationGizmo(painter);
    drawStatusBanner(painter);
}

void UsdViewportWidget::resizeEvent(QResizeEvent* event) {
    QWidget::resizeEvent(event);
    myImpl->hydraDirty = true;
    update();
}

void UsdViewportWidget::mousePressEvent(QMouseEvent* event) {
    setFocus();
    myImpl->lastMousePos = event->position();

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
        myImpl->panning = true;
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

    // 1. Check if user clicked on a 3D gizmo axis handle first
    const int hitAxis = hitTestGizmoAxis(event->position());
    if (hitAxis >= 0 && !myImpl->selectedPrimPath.isEmpty()) {
        pushUndoSnapshot();
        myImpl->draggingGizmo = true;
        myImpl->activeGizmoAxis = hitAxis;
        myImpl->dragStartPrimPos = selectedPrimPosition();
        myImpl->dragStartPrimRot = selectedPrimRotation();
        myImpl->dragStartPrimScale = selectedPrimScale();
        update();
        return;
    }

    // 2. Perform object picking: GPU picking if an active context exists; fast, crash-proof CPU ray picking otherwise
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
        selectPrim(hitPrimPath);
        QVector3D groundHit;
        if (screenToGroundPlane(event->position(), selectedPrimPosition().y(), groundHit)) {
            pushUndoSnapshot();
            myImpl->draggingObjectGround = true;
            myImpl->dragStartPrimPos = selectedPrimPosition();
            myImpl->dragStartWorldHit = groundHit;
        }
        return;
    }

    selectPrim(QString());
}

void UsdViewportWidget::mouseMoveEvent(QMouseEvent* event) {
    const QPointF delta = event->position() - myImpl->lastMousePos;
    myImpl->lastMousePos = event->position();

    if (myImpl->orbiting) {
        myImpl->yaw -= delta.x() * 0.008;
        myImpl->pitch = std::clamp(myImpl->pitch + delta.y() * 0.008, -1.45, 1.45);
        myImpl->hydraDirty = true;
        update();
        return;
    }

    if (myImpl->panning) {
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

    if (myImpl->draggingGizmo && myImpl->activeGizmoAxis >= 0 && !myImpl->selectedPrimPath.isEmpty()) {
        const int axis = myImpl->activeGizmoAxis;
        const float step = static_cast<float>((delta.x() - delta.y()) * 0.03);
        if (myImpl->gizmoMode == UsdGizmoMode::Move) {
            QVector3D pos = selectedPrimPosition();
            if (axis == 0) pos.setX(pos.x() + step);
            if (axis == 1) pos.setY(pos.y() + step);
            if (axis == 2) pos.setZ(pos.z() + step);
            setSelectedPrimPosition(pos);
        } else if (myImpl->gizmoMode == UsdGizmoMode::Rotate) {
            QVector3D rot = selectedPrimRotation();
            const float degStep = static_cast<float>((delta.x() - delta.y()) * 0.8);
            if (axis == 0) rot.setX(rot.x() + degStep);
            if (axis == 1) rot.setY(rot.y() + degStep);
            if (axis == 2) rot.setZ(rot.z() + degStep);
            setSelectedPrimRotation(rot);
        } else if (myImpl->gizmoMode == UsdGizmoMode::Scale) {
            const double s = std::max(0.05, selectedPrimScale() + (delta.x() - delta.y()) * 0.01);
            setSelectedPrimScale(s);
        }
        return;
    }

    if (myImpl->draggingObjectGround && !myImpl->selectedPrimPath.isEmpty()) {
        QVector3D groundHit;
        if (screenToGroundPlane(event->position(), myImpl->dragStartPrimPos.y(), groundHit)) {
            const QVector3D offset = groundHit - myImpl->dragStartWorldHit;
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

    myImpl->orbiting = false;
    myImpl->panning = false;
    myImpl->draggingGizmo = false;
    myImpl->draggingObjectGround = false;
    myImpl->paintingStroke = false;
    myImpl->activeGizmoAxis = -1;

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

void UsdViewportWidget::wheelEvent(QWheelEvent* event) {
    const double delta = event->angleDelta().y() / 120.0;
    if (std::abs(delta) < 1e-4) return;
    if (myImpl->useShotCam) {
        setUseShotCamera(false);
    }
    myImpl->distance = std::clamp(myImpl->distance * std::pow(0.88, delta), 1.0, 250.0);
    myImpl->hydraDirty = true;
    update();
}

void UsdViewportWidget::keyPressEvent(QKeyEvent* event) {
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
        case Qt::Key_W:
            setGizmoMode(UsdGizmoMode::Move);
            break;
        case Qt::Key_E:
            setGizmoMode(UsdGizmoMode::Rotate);
            break;
        case Qt::Key_R:
            setGizmoMode(UsdGizmoMode::Scale);
            break;
        case Qt::Key_F:
            frameSelectedPrim();
            break;
        case Qt::Key_Delete:
        case Qt::Key_Backspace:
            deleteSelectedPrim();
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

