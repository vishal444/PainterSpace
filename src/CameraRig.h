#pragma once

#include <QHash>
#include <QJsonObject>
#include <QPair>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QVector3D>

#pragma push_macro("emit")
#undef emit
#include "pxr/pxr.h"
#include "pxr/usd/usd/stage.h"
#pragma pop_macro("emit")

enum class CameraShotSize {
    ExtremeWide,
    Wide,
    Medium,
    MediumCloseUp,
    CloseUp,
    ExtremeCloseUp,
    OverTheShoulder,
    TopDown
};

enum class CameraAimMode {
    Free,
    LookAt,
    Frame,
    PathTangent
};

enum class CameraEaseType {
    Linear,
    EaseIn,
    EaseOut,
    EaseInOut
};

enum class CameraSensorPreset {
    Standard16_9,
    Super35,
    FullFrame
};

struct CameraRigPoint {
    QVector3D position;
    double timeS = -1.0;
    double focalLength = -1.0;
    double rollDeg = 0.0;
};

struct CameraSample {
    double timeS = 0.0;
    QVector3D position;
    QVector3D rotationDeg; // pitch, yaw, roll
    double focalLength = 35.0;
    double horizontalAperture = 36.0;
    double verticalAperture = 20.25;
    double focusDistance = 5.0;
    double fStop = 2.8;
};

struct CameraRigData {
    int schemaVersion = 1;
    QVector<CameraRigPoint> points;
    bool pathDrawn = false; // Only a completed user stroke creates a visible camera path.
    double pathStartS = 0.0;
    double pathEndS = 5.0;
    CameraEaseType ease = CameraEaseType::EaseInOut;
    CameraAimMode aimMode = CameraAimMode::LookAt;
    QStringList aimTargets;
    QString aimBone;
    QVector3D lookAtPoint = QVector3D(0.0f, 1.2f, 0.0f);
    bool hasCustomLookAt = false;
    QVector3D aimOffset = QVector3D(0.0f, 0.0f, 0.0f);
    double aimSmoothing = 0.2;
    CameraShotSize shotSize = CameraShotSize::Medium;
    QString shotSubject;
    bool framingMoveCamera = true; // true = move camera (dolly), false = change lens (zoom)
    double focalStart = 35.0;
    double focalEnd = 35.0;
    bool dofEnabled = false;
    double fStop = 2.8;
    double focusDistance = 5.0;
    double shakeAmp = 0.0;
    double shakeFreq = 2.0;
    CameraSensorPreset sensorPreset = CameraSensorPreset::Standard16_9;
    QString aspectRatio = QStringLiteral("16:9");
    bool dollyZoomEnabled = false;
};

class CameraRigEvaluator {
public:
    explicit CameraRigEvaluator(double fps = 24.0);

    CameraSample evaluate(
        const CameraRigData& rig,
        double timeS,
        const QHash<QString, QVector3D>& actorPositions = {},
        double subjectHeight = 1.8,
        double initialSubjectDist = -1.0) const;

    QVector<CameraSample> evaluateSequence(
        const CameraRigData& rig,
        double startS,
        double endS,
        const QHash<QString, QVector<QPair<double, QVector3D>>>& actorPositionsOverTime = {},
        double subjectHeight = 1.8) const;

    double computeShotSizeDistance(CameraShotSize size, double subjectHeight, double focalLength, double verticalAperture) const;
    double computeShotSizeFocal(CameraShotSize size, double subjectHeight, double distance, double verticalAperture) const;
    QVector3D computeLookAtRotation(const QVector3D& camPos, const QVector3D& targetPos, double rollDeg = 0.0) const;
    QVector3D computePathTangentRotation(const QVector3D& tangent, double rollDeg = 0.0) const;
    QVector<QVector3D> samplePolyline(const CameraRigData& rig, int samplesCount = 120) const;

    static double evaluateEase(double tNorm, CameraEaseType ease);
    static void sensorDimensions(CameraSensorPreset preset, double& outHoriz, double& outVert);
    static QString shotSizeToString(CameraShotSize size);
    static CameraShotSize stringToShotSize(const QString& str);
    static QString aimModeToString(CameraAimMode mode);
    static CameraAimMode stringToAimMode(const QString& str);
    static QString easeToString(CameraEaseType ease);
    static CameraEaseType stringToEase(const QString& str);
    static QString sensorPresetToString(CameraSensorPreset preset);
    static CameraSensorPreset stringToSensorPreset(const QString& str);

private:
    double myFps = 24.0;
};

// OpenUSD stage interaction & baking
bool saveCameraRigToStage(pxr::UsdStageRefPtr stage, const CameraRigData& rig, double startFrame, double endFrame, double fps);
bool loadCameraRigFromStage(pxr::UsdStageRefPtr stage, CameraRigData& outRig);
bool bakeCameraRigToShotCam(pxr::UsdStageRefPtr stage, const CameraRigData& rig, double startFrame, double endFrame, double fps,
                            const QHash<QString, QVector<QPair<double, QVector3D>>>& actorPositionsOverTime = {},
                            const QString& cameraPath = QStringLiteral("/World/ShotCam"));
bool resetCameraRigOnStage(pxr::UsdStageRefPtr stage);
bool convertShotCamToRig(pxr::UsdStageRefPtr stage, double startFrame, double endFrame, double fps, CameraRigData& outRig);

CameraRigData createCameraRigPreset(const QString& presetName, const QVector3D& targetPos = QVector3D(0.0f, 1.0f, 0.0f),
                                    double distance = 5.0, double durationS = 5.0, CameraShotSize shotSize = CameraShotSize::Medium);

QVector<QVector3D> simplifyPointsRdp(const QVector<QVector3D>& points, double epsilon = 0.05);

// Canonical v1 sidecar bridge. Unknown fields are retained for headless/editor parity.
QJsonObject cameraRigToJson(const CameraRigData& rig, const QJsonObject& previous = {});
CameraRigData cameraRigFromJson(const QJsonObject& camera);
