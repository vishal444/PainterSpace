#pragma once

#include <QByteArray>
#include <QHash>
#include <QImage>
#include <QJsonArray>
#include <QString>
#include <QStringList>
#include <QVector3D>

class QJsonObject;

class ShotEngine {
public:
    struct PlaybackPlan {
        QByteArray sceneSnapshot;
        double duration = 0.0;
        int fps = 24;
        QString shotSize;
        double lensMm = 50.0;
        QString cameraMovement;
        double startingCameraDistance = 9.5;
        double endingCameraDistance = 9.5;
        QJsonArray events;
        QHash<QString, int> assetIds;
        QHash<QString, QVector3D> startingPositions;
        QHash<QString, QVector3D> endingPositions;
        // Root-motion windows resolved from character locomotion.  These keep
        // the creative ShotSpec declarative while the preview remains a
        // deterministic timeline evaluator.
        QHash<QString, double> motionStartTimes;
        QHash<QString, double> motionEndTimes;
        bool bridgeShaking = false;
    };

    struct Result {
        bool success = false;
        QString error;
        QByteArray sceneSnapshot;
        QImage preview;
        PlaybackPlan playback;
    };

    struct UsdResult {
        bool success = false;
        QString error;
        QString stagePath;
    };

    struct UsdRenderResult {
        bool success = false;
        QString error;
        QStringList frames;
    };

    struct VideoResult {
        bool success = false;
        QString error;
        QString moviePath;
    };

    struct UsdStageInfo {
        bool valid = false;
        int startFrame = 0;
        int endFrame = 96;
        int fps = 24;
        double duration = 4.0;
        QString cameraPath = "/World/ShotCam";
        QString defaultPrim = "World";
    };

    static QString validateShotSpec(const QJsonObject& shotSpec);
    // Inspects a USD shot stage and optional beside-stage shot.json metadata.
    // If shot.json and shot.usda disagree, shot.usda wins for what renders.
    static UsdStageInfo inspectUsdStage(const QString& stagePath);
    // Validates untrusted, LLM-authored USDA as data. References must resolve
    // inside the repository asset library or scene layer; scripts and remote layers are rejected.
    static QString validateAuthoredUsdStage(const QString& stagePath);
    // Authors an OpenUSD ASCII stage from the application-level ShotSpec.
    static UsdResult buildOpenUsdStage(const QJsonObject& shotSpec, const QString& outputPath);
    // Renders one or more frames from an authored USD stage through the
    // installed Hydra Storm command-line renderer.
    static UsdRenderResult renderOpenUsdStage(const QString& stagePath, const QString& outputPattern,
                                              int startFrame, int endFrame, int width = 960,
                                              const QString& cameraPath = QString());
    // Encodes a numbered PNG sequence (for example, frame.###.png) as H.264 MP4.
    static VideoResult encodeFrameSequence(const QString& inputPattern, int fps, const QString& moviePath);

private:
    static QString validate(const QJsonObject& shotSpec);
};
