#pragma once

#include <QColor>
#include <QImage>
#include <QMatrix4x4>
#include <QPointF>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QVector3D>
#include <QWidget>
#include <memory>

#include "AssetTypes.h"
#include "CameraRig.h"

class QCheckBox;
class QComboBox;
class QFileSystemWatcher;
class QKeyEvent;
class QLabel;
class QMouseEvent;
class QPushButton;
class QSlider;
class QTimer;
class QWheelEvent;
class CameraToolController;

enum class ViewMode {
    World,
    CameraViewfinder
};

struct UsdPrimItem {
    QString path;
    QString name;
    QString typeName;
    QString assetId;
    QString definedIn; // "scene", "shot", or "user_edits"
    bool visible = true;
    bool hasUserEdit = false;
    int depth = 0;
    QString actorId;
    QString actorLabel;
    QString appearanceVariant;
    QColor identityColor = QColor("#5ca0d3");
};


class UsdViewportWidget : public QWidget {
    Q_OBJECT

public:
    explicit UsdViewportWidget(QWidget* parent = nullptr);
    ~UsdViewportWidget() override;

    // Actor Identity & Labeling (Shot Function Library Phase 0)
    bool renameActorLabel(const QString& primPath, const QString& newLabel);

    // Stage lifecycle (Phases 1 & 2)
    bool openStage(const QString& stagePath, bool isSceneStage = false);
    void reloadStage();
    QString stagePath() const;
    QString userEditsLayerPath() const;
    bool hasStage() const;
    bool isSceneStage() const;
    bool saveSceneEdits(QString* errorMsg = nullptr);
    int snapshotSceneForExistingShots(QString* snapshotFileName = nullptr);

    // Playback & timeline (Phases 0, 1, 4)
    void setPlaying(bool playing);
    bool isPlaying() const;
    double currentFrame() const;
    void setCurrentFrame(double frame);
    double startFrame() const;
    double endFrame() const;
    double fps() const;

    // Camera control & Director Mode (Phases 1, 2, 3, 4)
    void setUseShotCamera(bool useShotCam);
    bool useShotCamera() const;
    void setViewMode(ViewMode mode);
    ViewMode viewMode() const;
    void toggleViewMode();
    CameraToolController* cameraTools() const;
    void setShotSize(CameraShotSize size);
    CameraShotSize shotSize() const;
    void applyShotSizePreset(CameraShotSize size, bool moveCamera = true);
    void setCameraFocalLength(double focalMm);
    double cameraFocalLength() const;
    void setCameraAspectRatio(const QString& aspect);
    QString cameraAspectRatio() const;
    void resetCameraEdits();
    void convertLlmCameraToManualRig();
    void bakeCurrentCameraRig();
    QJsonObject cameraDocument() const;
    void setCameraDocument(const QJsonObject& document);
    QString captureUndoState() const;
    void restoreUndoState(const QString& snapshot);
    void snapCameraToView();
    void flushCameraPreview();
    QRectF viewfinderCropRect(double targetAspect) const;
    QString activeCameraPath() const;
    void resetFreeCamera();
    void frameSelectedPrim();
    QVector3D cameraEvaluatedPosition() const;
    QVector3D cameraEvaluatedRotation() const;
    QVector3D resolvedAimTargetPosition() const;
    void setAimTargetPosition(const QVector3D& pos);
    bool isAimTargetSelected() const;
    void setAimTargetSelected(bool selected);
    void resetAimTargetToCenter();

    // Camera Path & Joint manipulation
    void extendCameraPath();
    void deleteLastCameraPathJoint();
    void deleteSelectedCameraPathJoint();
    void clearCameraPath();
    int cameraPathJointCount() const;
    int selectedCameraPathJointIndex() const;
    void selectCameraPathJoint(int index);

    // General Asset Movement Paths (Characters, Vehicles, Animals, and Camera)
    bool isDrawPathActive() const;
    void startDrawingPath(const QString& primPath = QString());
    void stopDrawingPath();
    QString drawingPrimPath() const;
    bool hasPrimPath(const QString& primPath) const;
    QVector<QVector3D> primPath(const QString& primPath) const;
    void setPrimPath(const QString& primPath, const QVector<QVector3D>& points);
    void clearPrimPath(const QString& primPath);
    void clearAllPrimPaths();
    QColor primPathColor(const QString& primPath) const;
    QHash<QString, QVector<QVector3D>> allPrimPaths() const;
    QStringList explicitlyDrawnPrimPaths() const;
    void setExplicitlyDrawnPrimPaths(const QStringList& paths);
    void setAllPrimPaths(const QHash<QString, QVector<QVector3D>>& paths, bool recordUndo = false);
    void persistPrimPaths();
    void setDirectorBrief(const QJsonObject& brief);
    void setPathLint(const QStringList& paths);
    int primPathJointCount(const QString& primPath) const;

    // 3D Math & Picking public helpers
    QMatrix4x4 currentViewMatrix() const;
    QMatrix4x4 currentProjectionMatrix(double aspect) const;
    QPointF projectWorldToScreen(const QVector3D& worldPos, const QMatrix4x4& viewProj, bool* inFront = nullptr) const;
    bool screenToGroundPlane(const QPointF& screenPt, double planeY, QVector3D& outWorldPt) const;
    bool screenToRay(const QPointF& screenPt, QVector3D& outOrigin, QVector3D& outDir) const;
    bool tryCpuRayPick(const QPointF& screenPt, QString& outHitPrimPath, QVector3D* outHitPoint = nullptr) const;

    // Prim tree & selection (Phases 1 & 3)
    QVector<UsdPrimItem> primList() const;
    QString selectedPrimPath() const;
    void selectPrim(const QString& primPath);
    void setPrimVisible(const QString& primPath, bool visible);

    // Transform & authoring in user_edits.usda (Phase 3)
    QVector3D selectedPrimPosition() const;
    bool primWorldPosition(const QString& primPath, QVector3D& position) const;
    void setSelectedPrimPosition(const QVector3D& position);
    QVector3D selectedPrimRotation() const;
    void setSelectedPrimRotation(const QVector3D& rotationDeg);
    double selectedPrimScale() const;
    void setSelectedPrimScale(double scale);
    void deleteSelectedPrim();
    void duplicateSelectedPrim();
    QString addReferencedAsset(const QString& assetId, const QString& usdRelPath,
                               const QString& baseName, const QString& forwardAxis = QStringLiteral("-Z"),
                               double defaultScale = 1.0);
    QString addProceduralAsset(AssetType type, const QString& baseName);

    // User-edits layer management (Phase 3)
    bool userEditsEnabled() const;
    void setUserEditsEnabled(bool enabled);
    int userEditedPrimCount() const;
    QStringList orphanedUserEditPrims() const;
    void clearAllUserEdits();
    void clearSelectedPrimUserEdits();

    // Undo / Redo (Phase 3)
    bool canUndo() const;
    bool canRedo() const;
    void undo();
    void redo();
    void pushUndoSnapshot();

    // Tools, Gizmos, and Ground Paint (Phases 1, 3, 6)
    void setTool(PaintingTool tool);
    PaintingTool tool() const;
    void setBrushColor(const QColor& color);
    void setBrushSize(double size);
    void setBrushOpacity(double opacity);
    void setBackgroundColor(const QColor& color);
    QColor backgroundColor() const;
    void setGroundGridVisible(bool visible);
    bool groundGridVisible() const;
    void clearPaintStrokes();

    // Offscreen / clean render capture & status banners (Phases 1 & 2)
    QImage renderCurrentView(int width = 0, int height = 0);
    void setGenerationStatus(const QString& message, bool isError = false, bool isBusy = false);
    double lastFrameRenderMs() const;

signals:
    void selectionChanged(const QString& primPath);
    void stageChanged();
    void userEditsChanged(int editedPrimCount);
    void playbackFrameChanged(double frame);
    void statusMessageChanged(const QString& message);
    void viewModeChanged(ViewMode mode);
    void shotSizeChanged(CameraShotSize size);
    void primPathChanged(const QString& primPath);
    void drawPathModeChanged(bool active, const QString& primPath);
    void undoSnapshotPushed();

protected:
    void paintEvent(QPaintEvent* event) override;
    void resizeEvent(QResizeEvent* event) override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;
    void keyPressEvent(QKeyEvent* event) override;

private:
    struct Impl;
    std::unique_ptr<Impl> myImpl;

    void setupOverlayControls();
    void updateOverlayUi();
    void renderHydraToImage(int pixelWidth, int pixelHeight, QImage& outImage);
    void drawGridOverlay(QPainter& painter, const QMatrix4x4& viewProj) const;
    void drawOrbitCenterOverlay(QPainter& painter, const QMatrix4x4& viewProj) const;
    void drawSelectionAndGizmoOverlay(QPainter& painter, const QMatrix4x4& viewProj) const;
    void drawCameraFrustumAndPath(QPainter& painter, const QMatrix4x4& viewProj) const;
    void drawViewfinderHud(QPainter& painter) const;
    void drawActorNameTags(QPainter& painter, const QMatrix4x4& viewProj) const;
    void drawOrientationGizmo(QPainter& painter) const;
    void drawStatusBanner(QPainter& painter) const;
    void zoomFreeCamera(double steps, const QPointF& screenPos, bool dolly = false,
                        bool zoomToMouse = true);
    bool canUseGpuPicking() const;
    bool tryGpuPicking(const QPointF& screenPt, QString& outHitPrimPath, QVector3D* outHitPoint = nullptr) const;
    int hitTestGizmoAxis(const QPointF& screenPt) const;
    void syncWatchedFiles();
};

