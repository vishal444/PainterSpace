#include "ShotEngine.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QStandardPaths>
#include <QStringList>
#include <QTextStream>
#include <QVector3D>

#include <algorithm>

namespace {
bool isKnownLogicalAsset(const QString& asset) {
    static const QSet<QString> knownAssets{
        "character.hero", "vehicle.car", "environment.ocean", "environment.harbour",
        "vehicle.sedan",
        "environment.downtown", "environment.neighborhood", "environment.avenue",
        "environment.bridge", "prop.rock", "prop.tree", "prop.old_mobile_phone",
        "prop.car_keys"
    };
    return knownAssets.contains(asset);
}

QVector3D positionFor(const QJsonObject& asset) {
    const QJsonObject transform = asset.value("transform").toObject();
    const QJsonArray position = transform.value("position").toArray();
    if (position.size() != 3) return QVector3D();
    return QVector3D(static_cast<float>(position.at(0).toDouble()),
                     static_cast<float>(position.at(1).toDouble()),
                     static_cast<float>(position.at(2).toDouble()));
}

bool hasValidPosition(const QJsonObject& asset) {
    if (!asset.contains("transform")) return true;
    const QJsonArray position = asset.value("transform").toObject().value("position").toArray();
    return position.size() == 3 && position.at(0).isDouble() && position.at(1).isDouble() && position.at(2).isDouble();
}

QString eventSubject(const QString& action) {
    const int separator = action.indexOf('.');
    return separator > 0 ? action.left(separator) : QString();
}

int eventFrameFor(const QJsonArray& events, const QString& assetId, const QString& actionFragment, int fps) {
    for (const QJsonValue& value : events) {
        const QJsonObject event = value.toObject();
        const QString action = event.value("action").toString();
        if (eventSubject(action) == assetId && action.contains(actionFragment)) {
            return qRound(event.value("time").toDouble() * fps);
        }
    }
    return -1;
}

QVector3D animatedEndPosition(const QString& logicalAsset, const QString& assetId,
                              const QVector3D& start, const QJsonArray& events) {
    if (logicalAsset.startsWith("vehicle.") && eventFrameFor(events, assetId, "begin_moving", 1) >= 0) {
        return start + QVector3D(0.0f, 0.0f, -8.0f);
    }
    if (logicalAsset.startsWith("character.") &&
        (eventFrameFor(events, assetId, "enter", 1) >= 0 || eventFrameFor(events, assetId, "walk", 1) >= 0)) {
        return start + QVector3D(0.0f, 0.0f, 1.2f);
    }
    return start;
}

QJsonObject characterFor(const QJsonArray& characters, const QString& id) {
    for (const QJsonValue& value : characters) {
        const QJsonObject character = value.toObject();
        if (character.value("id").toString() == id) return character;
    }
    return {};
}

QVector3D locomotionEndPosition(const QJsonObject& character, const QVector3D& start,
                                const QHash<QString, QVector3D>& assetPositions) {
    const QJsonObject locomotion = character.value("locomotion").toObject();
    if (locomotion.isEmpty()) return start;
    const QJsonArray destination = locomotion.value("destination").toArray();
    if (destination.size() == 3) {
        return QVector3D(static_cast<float>(destination.at(0).toDouble()),
                         static_cast<float>(destination.at(1).toDouble()),
                         static_cast<float>(destination.at(2).toDouble()));
    }
    const QString target = locomotion.value("target").toString();
    if (!assetPositions.contains(target)) return start;
    const QVector3D targetPosition = assetPositions.value(target);
    // The actor stops short of a target asset rather than intersecting its
    // guide geometry.  Enter/interaction actions can use the same target.
    QVector3D direction = start - targetPosition;
    direction.setY(0.0f);
    if (direction.lengthSquared() < 0.001f) direction = QVector3D(0.0f, 0.0f, -1.0f);
    direction.normalize();
    return targetPosition + direction * 1.65f;
}

double motionStartFor(const QJsonObject& character) {
    return character.value("locomotion").toObject().value("start_time").toDouble(0.0);
}

double motionEndFor(const QJsonObject& character, double duration) {
    return character.value("locomotion").toObject().value("end_time").toDouble(duration);
}

QString expressionForPerformance(const QJsonObject& character) {
    const QString emotion = character.value("performance").toObject().value("emotion").toString().toLower();
    if (emotion == "fear" || emotion == "terrified") return "terrified";
    if (emotion == "sadness" || emotion == "sad") return "sad";
    if (emotion == "anger" || emotion == "angry") return "angry";
    if (emotion == "surprise" || emotion == "surprised") return "surprised";
    if (emotion == "joy" || emotion == "happy") return "happy";
    return "none";
}

int animationStartFrame(const QString& logicalAsset, const QString& assetId,
                        const QJsonArray& events, int fps) {
    if (logicalAsset.startsWith("vehicle.")) return eventFrameFor(events, assetId, "begin_moving", fps);
    if (logicalAsset.startsWith("character.")) {
        int frame = eventFrameFor(events, assetId, "enter", fps);
        return frame >= 0 ? frame : eventFrameFor(events, assetId, "walk", fps);
    }
    return -1;
}

QString animationClipFor(const QJsonArray& actors, const QString& assetId) {
    for (const QJsonValue& value : actors) {
        const QJsonObject actor = value.toObject();
        if (actor.value("id").toString() == assetId) {
            return actor.value("animation").toObject().value("clip").toString();
        }
    }
    return {};
}

double cameraDistanceFor(const QString& shotSize, double lensMm) {
    if (shotSize == "close_up" || lensMm >= 75.0) return 5.8;
    if (shotSize == "wide" || lensMm <= 35.0) return 14.0;
    return 9.5;
}

QString ffmpegPattern(QString pattern) {
    const QRegularExpression hashes("(#+)");
    const QRegularExpressionMatch match = hashes.match(pattern);
    if (!match.hasMatch()) return {};
    pattern.replace(match.capturedStart(), match.capturedLength(),
                    QString("%0%1d").arg('%').arg(match.capturedLength()));
    return pattern;
}

QString usdIdentifier(const QString& value, const QString& fallback) {
    QString identifier = value;
    identifier.replace(QRegularExpression("[^A-Za-z0-9_]"), "_");
    if (identifier.isEmpty()) identifier = fallback;
    if (identifier.front().isDigit()) identifier.prepend('_');
    return identifier;
}

QString usdString(QString value) {
    value.replace('\\', "\\\\");
    value.replace('"', "\\\"");
    value.replace('\n', "\\n");
    return value;
}

QString vectorLiteral(const QVector3D& vector) {
    return QString("(%1, %2, %3)")
        .arg(vector.x(), 0, 'f', 3)
        .arg(vector.y(), 0, 'f', 3)
        .arg(vector.z(), 0, 'f', 3);
}

void writeDisplayColor(QTextStream& stream, const QString& color) {
    stream << "                    color3f[] primvars:displayColor = [(" << color << ")]\n"
           << "                    uniform token primvars:displayColor:interpolation = \"constant\"\n";
}

void writeUsdPrevisGeometry(QTextStream& stream, const QString& logicalAsset, float fallbackSize) {
    // These are intentionally lightweight USD-native previs stand-ins.  They
    // make a stage legible in Storm while the asset registry is still being
    // upgraded to resolve final USD character and environment payloads.
    if (logicalAsset.startsWith("character.")) {
        stream << "                def Cylinder \"Torso\"\n                {\n"
               << "                    float height = 1.35\n                    float radius = 0.30\n"
               << "                    double3 xformOp:translate = (0, 1.55, 0)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\"]\n";
        writeDisplayColor(stream, "0.18, 0.42, 0.82");
        stream << "                }\n                def Sphere \"Head\"\n                {\n"
               << "                    float radius = 0.28\n                    double3 xformOp:translate = (0, 2.42, 0)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\"]\n";
        writeDisplayColor(stream, "0.94, 0.68, 0.48");
        stream << "                }\n";
        for (const QString& part : {QString("LeftLeg"), QString("RightLeg"), QString("LeftArm"), QString("RightArm")}) {
            const bool arm = part.contains("Arm");
            const bool left = part.contains("Left");
            const double x = left ? -0.38 : 0.38;
            const double y = arm ? 1.65 : 0.48;
            stream << "                def Cylinder \"" << part << "\"\n                {\n"
                   << "                    float height = " << (arm ? 1.05 : 1.10) << "\n"
                   << "                    float radius = " << (arm ? 0.11 : 0.14) << "\n"
                   << "                    double3 xformOp:translate = (" << x << ", " << y << ", 0)\n"
                   << "                    uniform token[] xformOpOrder = [\"xformOp:translate\"]\n";
            writeDisplayColor(stream, "0.18, 0.42, 0.82");
            stream << "                }\n";
        }
        return;
    }
    if (logicalAsset.startsWith("vehicle.")) {
        stream << "                def Cube \"Body\"\n                {\n"
               << "                    double3 xformOp:translate = (0, 0.58, 0)\n"
               << "                    double3 xformOp:scale = (1.45, 0.38, 0.72)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:scale\"]\n";
        writeDisplayColor(stream, "0.72, 0.12, 0.10");
        stream << "                }\n                def Cube \"Cabin\"\n                {\n"
               << "                    double3 xformOp:translate = (-0.18, 1.18, 0)\n"
               << "                    double3 xformOp:scale = (0.72, 0.27, 0.58)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:scale\"]\n";
        writeDisplayColor(stream, "0.16, 0.25, 0.32");
        stream << "                }\n";
        for (const QString& wheel : {QString("FrontLeftWheel"), QString("FrontRightWheel"), QString("RearLeftWheel"), QString("RearRightWheel")}) {
            const bool front = wheel.contains("Front");
            const bool left = wheel.contains("Left");
            stream << "                def Cylinder \"" << wheel << "\"\n                {\n"
                   << "                    float height = 0.20\n                    float radius = 0.34\n"
                   << "                    double3 xformOp:translate = (" << (front ? 0.92 : -0.92) << ", 0.34, " << (left ? -0.74 : 0.74) << ")\n"
                   << "                    float3 xformOp:rotateXYZ = (90, 0, 0)\n"
                   << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:rotateXYZ\"]\n";
            writeDisplayColor(stream, "0.035, 0.04, 0.05");
            stream << "                }\n";
        }
        return;
    }
    if (logicalAsset == "environment.bridge" || logicalAsset == "environment.avenue") {
        stream << "                def Cube \"BridgeDeck\"\n                {\n"
               << "                    double3 xformOp:translate = (0, 0.05, 0)\n"
               << "                    double3 xformOp:scale = (2.8, 0.12, 7.5)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:scale\"]\n";
        writeDisplayColor(stream, "0.22, 0.25, 0.28");
        stream << "                }\n";
        for (const QString& rail : {QString("LeftRail"), QString("RightRail")}) {
            stream << "                def Cube \"" << rail << "\"\n                {\n"
                   << "                    double3 xformOp:translate = (" << (rail.startsWith("Left") ? -2.58 : 2.58) << ", 0.68, 0)\n"
                   << "                    double3 xformOp:scale = (0.06, 0.25, 7.5)\n"
                   << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:scale\"]\n";
            writeDisplayColor(stream, "0.68, 0.55, 0.26");
            stream << "                }\n";
        }
        return;
    }
    if (logicalAsset == "prop.car_keys") {
        stream << "                def Cylinder \"KeyRing\"\n                {\n"
               << "                    float height = 0.08\n                    float radius = 0.16\n"
               << "                    double3 xformOp:translate = (0, 0.08, 0)\n"
               << "                    float3 xformOp:rotateXYZ = (90, 0, 0)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:rotateXYZ\"]\n";
        writeDisplayColor(stream, "0.94, 0.72, 0.18");
        stream << "                }\n                def Cube \"KeyBlade\"\n                {\n"
               << "                    double3 xformOp:translate = (0.24, 0.08, 0)\n"
               << "                    double3 xformOp:scale = (0.22, 0.035, 0.07)\n"
               << "                    uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:scale\"]\n";
        writeDisplayColor(stream, "0.94, 0.72, 0.18");
        stream << "                }\n";
        return;
    }
    stream << "                def Cube \"PrevisGeometry\"\n                {\n"
           << "                    float size = " << fallbackSize << "\n";
    writeDisplayColor(stream, "0.55, 0.60, 0.64");
    stream << "                }\n";
}

QVector3D cameraStartFor(const QString& shotSize, double lensMm) {
    if (shotSize == "close_up" || lensMm >= 75.0) return QVector3D(2.4f, 1.8f, 4.6f);
    if (shotSize == "wide" || lensMm <= 35.0) return QVector3D(6.5f, 3.1f, 11.0f);
    return QVector3D(4.6f, 2.4f, 7.5f);
}

QVector3D cameraEndFor(const QString& movement, const QVector3D& start) {
    if (movement.contains("push") || movement.contains("track") || movement.contains("dolly")) {
        return start * 0.58f;
    }
    if (movement.contains("orbit")) return QVector3D(-start.z() * 0.55f, start.y(), start.x() * 0.55f);
    if (movement.contains("pan")) return start;
    return start;
}
}

QString ShotEngine::validate(const QJsonObject& shotSpec) {
    const QStringList required = {"shot_id", "format_version", "duration", "fps", "assets", "camera", "events"};
    for (const QString& field : required) {
        if (!shotSpec.contains(field)) return QString("ShotSpec is missing %1").arg(field);
    }
    if (shotSpec.value("shot_id").toString().trimmed().isEmpty()) return "ShotSpec shot_id must not be empty";
    if (shotSpec.value("format_version").toString() != "1.0") return "Unsupported ShotSpec format_version";
    if (!shotSpec.value("duration").isDouble() || shotSpec.value("duration").toDouble() <= 0.0 ||
        shotSpec.value("duration").toDouble() > 600.0) return "ShotSpec duration must be between 0 and 600 seconds";
    if (!shotSpec.value("fps").isDouble() || shotSpec.value("fps").toInt() <= 0 ||
        shotSpec.value("fps").toInt() > 120) return "ShotSpec fps must be between 1 and 120";
    if (!shotSpec.value("assets").isArray()) return "ShotSpec assets must be an array";
    if (!shotSpec.value("camera").isObject()) return "ShotSpec camera must be an object";
    if (!shotSpec.value("events").isArray()) return "ShotSpec events must be an array";

    QSet<QString> assetIds;
    QHash<QString, QString> logicalAssets;
    for (const QJsonValue& value : shotSpec.value("assets").toArray()) {
        if (!value.isObject()) return "Each ShotSpec asset must be an object";
        const QJsonObject asset = value.toObject();
        const QString id = asset.value("id").toString().trimmed();
        const QString logicalAsset = asset.value("asset").toString();
        if (id.isEmpty() || logicalAsset.isEmpty()) return "Each ShotSpec asset requires id and asset";
        if (assetIds.contains(id)) return QString("Duplicate ShotSpec asset id: %1").arg(id);
        if (!isKnownLogicalAsset(logicalAsset)) return QString("Unsupported logical asset: %1").arg(logicalAsset);
        if (!hasValidPosition(asset)) return QString("Asset %1 has an invalid transform.position").arg(id);
        assetIds.insert(id);
        logicalAssets.insert(id, logicalAsset);
    }

    // Characters express intent and constraints here; the engine owns the
    // lower-level body and path solution.  All fields are optional for
    // backward-compatible v1.0 shots, but strict whenever they are supplied.
    if (shotSpec.contains("characters") && !shotSpec.value("characters").isArray()) {
        return "ShotSpec characters must be an array";
    }
    static const QSet<QString> supportedActions{
        "idle", "walk", "run", "sprint", "turn", "stop", "reach", "grab", "open", "close",
        "sit", "stand", "look", "search", "pick_up", "drop", "enter_vehicle", "exit_vehicle",
        "struggle"
    };
    QSet<QString> characterIds;
    const double duration = shotSpec.value("duration").toDouble();
    for (const QJsonValue& value : shotSpec.value("characters").toArray()) {
        if (!value.isObject()) return "Each ShotSpec character must be an object";
        const QJsonObject character = value.toObject();
        const QString id = character.value("id").toString();
        if (!logicalAssets.value(id).startsWith("character.")) {
            return QString("Character %1 must reference a character asset").arg(id);
        }
        if (characterIds.contains(id)) return QString("Duplicate ShotSpec character id: %1").arg(id);
        characterIds.insert(id);
        if (character.contains("intent") && !character.value("intent").isString()) {
            return QString("Character %1 intent must be a string").arg(id);
        }
        const QJsonObject locomotion = character.value("locomotion").toObject();
        if (character.contains("locomotion") && locomotion.isEmpty()) {
            return QString("Character %1 locomotion must be an object").arg(id);
        }
        if (!locomotion.isEmpty()) {
            const QString style = locomotion.value("style").toString();
            if (!style.isEmpty() && !supportedActions.contains(style)) {
                return QString("Unsupported locomotion style for %1: %2").arg(id, style);
            }
            const double start = locomotion.value("start_time").toDouble(-1.0);
            const double end = locomotion.value("end_time").toDouble(-1.0);
            if (!locomotion.value("start_time").isDouble() || !locomotion.value("end_time").isDouble() ||
                start < 0.0 || end <= start || end > duration) {
                return QString("Character %1 locomotion must have a valid start_time and end_time").arg(id);
            }
            const QString target = locomotion.value("target").toString();
            const QJsonArray destination = locomotion.value("destination").toArray();
            if (target.isEmpty() && destination.size() != 3) {
                return QString("Character %1 locomotion needs a target or 3D destination").arg(id);
            }
            if (!target.isEmpty() && !assetIds.contains(target)) {
                return QString("Character %1 locomotion target is unknown: %2").arg(id, target);
            }
            if (!destination.isEmpty() && (destination.size() != 3 || !destination.at(0).isDouble() ||
                !destination.at(1).isDouble() || !destination.at(2).isDouble())) {
                return QString("Character %1 locomotion destination must be [x, y, z]").arg(id);
            }
        }
        if (character.contains("actions") && !character.value("actions").isArray()) {
            return QString("Character %1 actions must be an array").arg(id);
        }
        for (const QJsonValue& actionValue : character.value("actions").toArray()) {
            const QJsonObject action = actionValue.toObject();
            const QString name = action.value("name").toString();
            if (!supportedActions.contains(name)) return QString("Unsupported character action: %1").arg(name);
            const double start = action.value("start_time").toDouble(-1.0);
            const double end = action.value("end_time").toDouble(-1.0);
            if (!action.value("start_time").isDouble() || !action.value("end_time").isDouble() ||
                start < 0.0 || end < start || end > duration) {
                return QString("Character action %1 has invalid timing").arg(name);
            }
            const QString target = action.value("target").toString();
            if (!target.isEmpty() && !assetIds.contains(target)) {
                return QString("Character action %1 targets unknown asset %2").arg(name, target);
            }
        }
        const QJsonObject gaze = character.value("gaze").toObject();
        if (character.contains("gaze") && (gaze.isEmpty() || !assetIds.contains(gaze.value("target").toString()))) {
            return QString("Character %1 gaze must target a known asset").arg(id);
        }
        const QJsonObject performance = character.value("performance").toObject();
        for (const QString& amount : {QString("intensity"), QString("urgency")}) {
            if (performance.contains(amount) && (!performance.value(amount).isDouble() ||
                performance.value(amount).toDouble() < 0.0 || performance.value(amount).toDouble() > 1.0)) {
                return QString("Character %1 performance %2 must be between 0 and 1").arg(id, amount);
            }
        }
    }

    const QJsonObject camera = shotSpec.value("camera").toObject();
    const double lensMm = camera.value("lens_mm").toDouble();
    static const QSet<QString> supportedMovements{
        "locked_off", "slow_push_in", "slow_tracking_push_in", "tracking_push_in", "dolly_in", "pan", "orbit"
    };
    if (camera.value("shot_size").toString().isEmpty()) return "Camera shot_size is required";
    if (!camera.value("lens_mm").isDouble() || lensMm < 8.0 || lensMm > 300.0) {
        return "Camera lens_mm must be between 8 and 300";
    }
    if (!supportedMovements.contains(camera.value("movement").toString("locked_off").toLower())) {
        return QString("Unsupported camera movement: %1").arg(camera.value("movement").toString());
    }
    const QString cameraTarget = camera.value("target").toString();
    if (!cameraTarget.isEmpty() && !assetIds.contains(cameraTarget)) {
        return QString("Camera target does not reference an asset: %1").arg(cameraTarget);
    }

    for (const QJsonValue& value : shotSpec.value("events").toArray()) {
        if (!value.isObject()) return "Each ShotSpec event must be an object";
        const QJsonObject event = value.toObject();
        const QString action = event.value("action").toString();
        const double time = event.value("time").toDouble(-1.0);
        if (action.isEmpty() || !event.value("time").isDouble()) return "Each ShotSpec event requires time and action";
        if (time < 0.0 || time > duration) return QString("Event %1 is outside the shot duration").arg(action);
        const QString subject = eventSubject(action);
        if (!subject.isEmpty() && subject != "camera" && !assetIds.contains(subject)) {
            return QString("Event references an unknown asset: %1").arg(subject);
        }
    }
    return {};
}

QString ShotEngine::validateShotSpec(const QJsonObject& shotSpec) {
    return validate(shotSpec);
}



namespace {
QStringList extractCameraPaths(const QString& source) {
    QStringList cameras;
    struct PrimEntry {
        int depth = 0;
        QString name;
    };
    QVector<PrimEntry> stack;
    QString pendingSpec;
    QString pendingType;
    QString pendingName;
    int braceDepth = 0;
    int parenDepth = 0;

    const QRegularExpression primHeader("^\\s*(def|over|class)\\s+(?:([A-Za-z0-9_]+)\\s+)?\"([^\"]+)\"");
    const QStringList lines = source.split('\n');
    for (const QString& rawLine : lines) {
        QString line = rawLine;
        const int commentIdx = line.indexOf('#');
        if (commentIdx >= 0) line = line.left(commentIdx);

        if (parenDepth == 0) {
            const QRegularExpressionMatch match = primHeader.match(line);
            if (match.hasMatch()) {
                pendingSpec = match.captured(1);
                pendingType = match.captured(2);
                pendingName = match.captured(3);
            }
        }

        bool inQuotes = false;
        for (int i = 0; i < line.size(); ++i) {
            const QChar ch = line.at(i);
            if (ch == '"' && (i == 0 || line.at(i - 1) != '\\')) {
                inQuotes = !inQuotes;
                continue;
            }
            if (inQuotes) continue;
            if (ch == '(') {
                ++parenDepth;
            } else if (ch == ')') {
                parenDepth = qMax(0, parenDepth - 1);
            } else if (ch == '=' && parenDepth == 0) {
                pendingName.clear();
            } else if (ch == '{' && parenDepth == 0) {
                ++braceDepth;
                if (!pendingName.isEmpty()) {
                    stack.push_back({braceDepth, pendingName});
                    if (pendingSpec == "def" && pendingType == "Camera") {
                        QStringList parts;
                        for (const PrimEntry& entry : stack) parts.append(entry.name);
                        cameras.append("/" + parts.join('/'));
                    }
                    pendingName.clear();
                }
            } else if (ch == '}' && parenDepth == 0) {
                while (!stack.isEmpty() && stack.back().depth == braceDepth) {
                    stack.pop_back();
                }
                braceDepth = qMax(0, braceDepth - 1);
            }
        }
    }
    return cameras;
}

QStringList findAssetRootsForStage(const QString& stageDirectory) {
    QStringList roots;
    QDir cursor(stageDirectory);
    for (int depth = 0; depth < 8; ++depth) {
        const QString candidate = QFileInfo(cursor.filePath("assets")).canonicalFilePath();
        if (!candidate.isEmpty() && !roots.contains(candidate)) {
            roots.append(candidate);
        }
        if (!cursor.cdUp()) break;
    }
    return roots;
}
}

ShotEngine::UsdStageInfo ShotEngine::inspectUsdStage(const QString& stagePath) {
    UsdStageInfo info;
    const QFileInfo fileInfo(stagePath);
    if (!fileInfo.exists() || !fileInfo.isFile()) return info;

    // 1. Load fallback metadata from shot.json (or <stem>.json) beside the stage.
    QString metadataCamera;
    QStringList metadataCandidates{
        fileInfo.dir().filePath("shot.json"),
        fileInfo.dir().filePath(fileInfo.completeBaseName() + ".json")
    };
    metadataCandidates.removeDuplicates();
    for (const QString& metaPath : metadataCandidates) {
        QFile metaFile(metaPath);
        if (!metaFile.open(QIODevice::ReadOnly)) continue;
        const QJsonDocument doc = QJsonDocument::fromJson(metaFile.readAll());
        if (!doc.isObject()) continue;
        const QJsonObject meta = doc.object();
        if (meta.value("fps").isDouble()) {
            info.fps = qBound(1, meta.value("fps").toInt(24), 120);
        }
        if (meta.value("frame_range").isArray()) {
            const QJsonArray range = meta.value("frame_range").toArray();
            if (range.size() == 2) {
                info.startFrame = range.at(0).toInt(0);
                info.endFrame = range.at(1).toInt(info.endFrame);
            }
        } else {
            if (meta.value("start_frame").isDouble()) info.startFrame = meta.value("start_frame").toInt(0);
            if (meta.value("end_frame").isDouble()) info.endFrame = meta.value("end_frame").toInt(info.endFrame);
        }
        if (meta.value("duration").isDouble() && !meta.contains("end_frame") && !meta.contains("frame_range")) {
            info.duration = meta.value("duration").toDouble(4.0);
            info.endFrame = info.startFrame + qRound(info.duration * info.fps);
        } else if (meta.value("duration_seconds").isDouble() && !meta.contains("end_frame") && !meta.contains("frame_range")) {
            info.duration = meta.value("duration_seconds").toDouble(4.0);
            info.endFrame = info.startFrame + qRound(info.duration * info.fps);
        }
        const QString cam = meta.value("camera_prim").toString(
            meta.value("camera_prim_path").toString(meta.value("camera").toString()));
        if (!cam.isEmpty()) {
            metadataCamera = cam;
            info.cameraPath = cam;
        }
        break;
    }

    // 2. Read shot.usda directly. If shot.json and shot.usda disagree, shot.usda wins.
    QFile stageFile(stagePath);
    if (!stageFile.open(QIODevice::ReadOnly)) return info;
    const QString source = QString::fromUtf8(stageFile.readAll());
    if (!source.startsWith("#usda 1.0")) return info;

    const auto defaultPrimMatch = QRegularExpression("defaultPrim\\s*=\\s*\"([^\"]+)\"").match(source);
    if (defaultPrimMatch.hasMatch()) info.defaultPrim = defaultPrimMatch.captured(1);

    const auto rateMatch = QRegularExpression("(?:timeCodesPerSecond|framesPerSecond)\\s*=\\s*(\\d+(?:\\.\\d+)?)").match(source);
    if (rateMatch.hasMatch()) info.fps = qBound(1, qRound(rateMatch.captured(1).toDouble()), 120);

    const auto startMatch = QRegularExpression("startTimeCode\\s*=\\s*(\\d+(?:\\.\\d+)?)").match(source);
    if (startMatch.hasMatch()) info.startFrame = qRound(startMatch.captured(1).toDouble());

    const auto endMatch = QRegularExpression("endTimeCode\\s*=\\s*(\\d+(?:\\.\\d+)?)").match(source);
    if (endMatch.hasMatch()) info.endFrame = qRound(endMatch.captured(1).toDouble());

    if (info.endFrame < info.startFrame) info.endFrame = info.startFrame;
    info.duration = info.fps > 0 ? static_cast<double>(info.endFrame - info.startFrame) / info.fps : 0.0;

    QStringList cameras = extractCameraPaths(source);
    if (cameras.isEmpty()) {
        const QString sceneLayer = fileInfo.dir().filePath("../scene.usda");
        QFile sceneFile(sceneLayer);
        if (sceneFile.open(QIODevice::ReadOnly)) {
            cameras = extractCameraPaths(QString::fromUtf8(sceneFile.readAll()));
        }
    }
    if (!cameras.isEmpty()) {
        if (!metadataCamera.isEmpty() && cameras.contains(metadataCamera)) {
            info.cameraPath = metadataCamera;
        } else {
            info.cameraPath = cameras.first();
        }
    } else if (info.cameraPath.isEmpty() || !info.cameraPath.startsWith('/')) {
        const QString camName = info.cameraPath.isEmpty() ? QStringLiteral("ShotCam") : info.cameraPath;
        info.cameraPath = QString("/%1/%2").arg(info.defaultPrim.isEmpty() ? QStringLiteral("World") : info.defaultPrim, camName);
    }

    info.valid = true;
    return info;
}

QString ShotEngine::validateAuthoredUsdStage(const QString& stagePath) {
    const QFileInfo info(stagePath);
    if (!info.exists() || !info.isFile()) return "USD stage does not exist";
    if (info.size() <= 0 || info.size() > 8 * 1024 * 1024) return "USD stage must be between 1 byte and 8 MiB";
    QFile file(stagePath);
    if (!file.open(QIODevice::ReadOnly)) return "Unable to read USD stage";
    const QByteArray bytes = file.readAll();
    if (!bytes.startsWith("#usda 1.0")) return "Only OpenUSD ASCII (.usda) stages are accepted";
    const QString source = QString::fromUtf8(bytes);
    int primCount = 0;
    auto prims = QRegularExpression("\\b(def|over|class)\\s+(?:[A-Za-z0-9_]+\\s+)?\"").globalMatch(source);
    while (prims.hasNext()) { prims.next(); ++primCount; }
    if (primCount > 10000) return "USD stage exceeds the 10,000 prim limit";
    const QRegularExpression frameRate("(?:timeCodesPerSecond|framesPerSecond)\\s*=\\s*(\\d+(?:\\.\\d+)?)");
    const QRegularExpression frameEnd("endTimeCode\\s*=\\s*(\\d+(?:\\.\\d+)?)");
    const auto fpsMatch = frameRate.match(source);
    const auto endMatch = frameEnd.match(source);
    if (fpsMatch.hasMatch() && endMatch.hasMatch() &&
        fpsMatch.captured(1).toDouble() * endMatch.captured(1).toDouble() > 72000.0)
        return "USD stage exceeds the 600-second / 120-fps render budget";
    const QRegularExpression references("@([^@]+)@");
    auto it = references.globalMatch(source);
    const QString stageDirectory = info.absolutePath();
    const QStringList assetRoots = findAssetRootsForStage(stageDirectory);
    QString sceneDir;
    if (info.fileName() == "shot.usda" && info.dir().dirName().startsWith("shot_")) {
        sceneDir = QFileInfo(QDir(stageDirectory).filePath("..")).canonicalFilePath();
    } else if (info.fileName() == "scene.usda") {
        sceneDir = info.canonicalPath();
    }
    while (it.hasNext()) {
        const QString reference = it.next().captured(1);
        if (reference.isEmpty() || QDir::isAbsolutePath(reference) ||
            reference.contains("://") || reference.startsWith("/") ||
            !(reference.endsWith(".usd", Qt::CaseInsensitive) || reference.endsWith(".usda", Qt::CaseInsensitive) ||
              reference.endsWith(".usdc", Qt::CaseInsensitive)))
            return QString("Unsafe USD asset reference: %1").arg(reference);
        const QString fullPath = QFileInfo(QDir(stageDirectory).filePath(reference)).canonicalFilePath();
        if (fullPath.isEmpty()) {
            return QString("USD reference does not exist: %1").arg(reference);
        }
        bool allowedInAssets = false;
        for (const QString& root : assetRoots) {
            if (fullPath.startsWith(root + QDir::separator())) {
                allowedInAssets = true;
                break;
            }
        }
        const bool allowedSceneSublayer = !sceneDir.isEmpty() &&
            (fullPath == QFileInfo(QDir(sceneDir).filePath("scene.usda")).canonicalFilePath() ||
             (fullPath.startsWith(sceneDir + QDir::separator()) && fullPath.endsWith("/shot.usda")));
        if (!allowedInAssets && !allowedSceneSublayer) {
            return QString("USD asset reference is missing or outside assets/: %1").arg(reference);
        }
    }
    const QString usdcat = QStandardPaths::findExecutable("usdcat");
    if (usdcat.isEmpty()) return "usdcat is required to validate an authored USD stage";
    QProcess validator;
    validator.start(usdcat, {"--loadOnly", info.absoluteFilePath()});
    if (!validator.waitForFinished(30000) || validator.exitCode() != 0)
        return QString("usdcat rejected the authored stage: %1")
            .arg(QString::fromUtf8(validator.readAllStandardError()).trimmed());
    const QString checker = QStandardPaths::findExecutable("usdchecker");
    if (!checker.isEmpty()) {
        QProcess checkerProcess;
        checkerProcess.start(checker, {info.absoluteFilePath()});
        if (!checkerProcess.waitForFinished(30000) || checkerProcess.exitCode() != 0)
            return QString("usdchecker rejected the authored stage: %1")
                .arg(QString::fromUtf8(checkerProcess.readAllStandardError()).trimmed());
    }
    return {};
}

ShotEngine::UsdResult ShotEngine::buildOpenUsdStage(const QJsonObject& shotSpec, const QString& outputPath) {
    UsdResult result;
    if (const QString error = validate(shotSpec); !error.isEmpty()) {
        result.error = error;
        return result;
    }
    if (outputPath.isEmpty()) {
        result.error = "An output path is required for an OpenUSD stage";
        return result;
    }

    const QFileInfo outputInfo(outputPath);
    if (!QDir().mkpath(outputInfo.absolutePath())) {
        result.error = QString("Unable to create OpenUSD output directory: %1").arg(outputInfo.absolutePath());
        return result;
    }

    const double duration = shotSpec.value("duration").toDouble();
    const int fps = shotSpec.value("fps").toInt();
    const int endFrame = qRound(duration * fps);
    const QJsonObject camera = shotSpec.value("camera").toObject();
    const QString shotSize = camera.value("shot_size").toString().toLower();
    const double lensMm = camera.value("lens_mm").toDouble(50.0);
    const QString movement = camera.value("movement").toString("locked_off").toLower();
    const QVector3D cameraStart = cameraStartFor(shotSize, lensMm);
    const QVector3D cameraEnd = cameraEndFor(movement, cameraStart);
    const QJsonObject effects = shotSpec.value("effects").toObject();
    const QJsonArray events = shotSpec.value("events").toArray();
    const QJsonArray actors = shotSpec.value("actors").toArray();
    const QJsonArray characters = shotSpec.value("characters").toArray();
    QHash<QString, QVector3D> declaredPositions;
    for (const QJsonValue& value : shotSpec.value("assets").toArray()) {
        const QJsonObject asset = value.toObject();
        declaredPositions.insert(asset.value("id").toString(), positionFor(asset));
    }

    QSaveFile stage(outputPath);
    if (!stage.open(QIODevice::WriteOnly | QIODevice::Text)) {
        result.error = QString("Unable to write OpenUSD stage: %1").arg(outputPath);
        return result;
    }

    QTextStream stream(&stage);
    stream.setEncoding(QStringConverter::Utf8);
    stream << "#usda 1.0\n"
           << "(\n"
           << "    defaultPrim = \"Shot\"\n"
           << "    startTimeCode = 0\n"
           << "    endTimeCode = " << endFrame << "\n"
           << "    timeCodesPerSecond = " << fps << "\n"
           << "    framesPerSecond = " << fps << "\n"
           << "    upAxis = \"Y\"\n"
           << "    metersPerUnit = 1\n"
           << ")\n\n"
           << "def Xform \"Shot\"\n{\n"
           << "    custom string application:shotId = \"" << usdString(shotSpec.value("shot_id").toString()) << "\"\n"
           << "    custom string application:formatVersion = \"" << usdString(shotSpec.value("format_version").toString()) << "\"\n"
           << "    custom string application:source = \"ShotSpec\"\n\n"
           << "    def Xform \"World\"\n    {\n"
           << "        def DomeLight \"NightAmbient\"\n        {\n"
           << "            color3f inputs:color = (0.15, 0.22, 0.32)\n"
           << "            float inputs:intensity = 0.35\n"
           << "        }\n"
           << "        def DistantLight \"MoonKey\"\n        {\n"
           << "            color3f inputs:color = (0.55, 0.68, 1.0)\n"
           << "            float inputs:intensity = 1.2\n"
           << "            float angle = 0.53\n"
           << "            float3 xformOp:rotateXYZ = (-35, -20, 0)\n"
           << "            uniform token[] xformOpOrder = [\"xformOp:rotateXYZ\"]\n"
           << "        }\n\n"
           << "        def Xform \"Assets\"\n        {\n";

    const QJsonArray assets = shotSpec.value("assets").toArray();
    for (int index = 0; index < assets.size(); ++index) {
        const QJsonObject asset = assets.at(index).toObject();
        const QString id = usdIdentifier(asset.value("id").toString(), QString("Asset_%1").arg(index + 1));
        const QString assetId = asset.value("asset").toString();
        const QVector3D position = positionFor(asset);
        QVector3D endPosition = animatedEndPosition(assetId, asset.value("id").toString(), position, events);
        int moveStart = animationStartFrame(assetId, asset.value("id").toString(), events, fps);
        int moveEnd = endFrame;
        const QString animationClip = animationClipFor(actors, asset.value("id").toString());
        const QJsonObject character = characterFor(characters, asset.value("id").toString());
        if (!character.isEmpty()) {
            endPosition = locomotionEndPosition(character, position, declaredPositions);
            moveStart = qRound(motionStartFor(character) * fps);
            moveEnd = qRound(motionEndFor(character, duration) * fps);
        }
        const float proxySize = assetId.startsWith("character.") ? 1.8f
                              : assetId.startsWith("vehicle.") ? 3.5f : 0.35f;
        stream << "            def Xform \"" << id << "\"\n            {\n"
               << "                custom string application:logicalAsset = \"" << usdString(assetId) << "\"\n"
               << "                custom string application:assetId = \"" << usdString(asset.value("id").toString()) << "\"\n";
        if (!animationClip.isEmpty()) {
            stream << "                custom string application:animationClip = \"" << usdString(animationClip) << "\"\n";
        }
        if (!character.isEmpty()) {
            const QJsonObject locomotion = character.value("locomotion").toObject();
            const QJsonObject performance = character.value("performance").toObject();
            const QJsonObject gaze = character.value("gaze").toObject();
            stream << "                custom string application:intent = \""
                   << usdString(character.value("intent").toString()) << "\"\n";
            if (!locomotion.isEmpty()) {
                stream << "                custom string application:locomotionStyle = \""
                       << usdString(locomotion.value("style").toString()) << "\"\n"
                       << "                custom string application:locomotionTarget = \""
                       << usdString(locomotion.value("target").toString()) << "\"\n";
            }
            if (!performance.isEmpty()) {
                stream << "                custom string application:emotion = \""
                       << usdString(performance.value("emotion").toString()) << "\"\n"
                       << "                custom float application:performanceIntensity = "
                       << performance.value("intensity").toDouble() << "\n"
                       << "                custom float application:urgency = "
                       << performance.value("urgency").toDouble() << "\n";
            }
            if (!gaze.isEmpty()) {
                stream << "                custom string application:gazeTarget = \""
                       << usdString(gaze.value("target").toString()) << "\"\n";
            }
        }
        if (moveStart >= 0 && endPosition != position) {
            stream << "                double3 xformOp:translate.timeSamples = {\n"
                   << "                    0: " << vectorLiteral(position) << ",\n"
                   << "                    " << moveStart << ": " << vectorLiteral(position) << ",\n"
                   << "                    " << moveEnd << ": " << vectorLiteral(endPosition) << "\n"
                   << "                }\n";
        } else {
            stream << "                double3 xformOp:translate = " << vectorLiteral(position) << "\n";
        }
        stream << "                uniform token[] xformOpOrder = [\"xformOp:translate\"]\n";
        writeUsdPrevisGeometry(stream, assetId, proxySize);
        stream << "            }\n";
    }

    stream << "        }\n\n"
           << "        def Xform \"CameraRig\"\n        {\n"
           << "            custom string application:movement = \"" << usdString(movement) << "\"\n"
           << "            double3 xformOp:translate.timeSamples = {\n"
           << "                0: " << vectorLiteral(cameraStart) << ",\n"
           << "                " << endFrame << ": " << vectorLiteral(cameraEnd) << "\n"
           << "            }\n"
           << "            float3 xformOp:rotateXYZ.timeSamples = {\n"
           << "                0: (-12, 31, 0),\n"
           << "                " << endFrame << ": (-9, 31, 0)\n"
           << "            }\n"
           << "            uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:rotateXYZ\"]\n"
           << "            def Camera \"RenderCamera\"\n            {\n"
           << "                float focalLength = " << lensMm << "\n"
           << "                float horizontalAperture = 36\n"
           << "                float verticalAperture = 20.25\n"
           << "                custom string application:target = \"" << usdString(camera.value("target").toString()) << "\"\n"
           << "                custom string application:focusTarget = \"" << usdString(camera.value("focus_target").toString()) << "\"\n"
           << "            }\n"
           << "        }\n\n"
           << "        def Scope \"Effects\"\n        {\n"
           << "            custom bool application:rain = " << (effects.value("rain").toBool() ? "true" : "false") << "\n"
           << "            custom bool application:lightFlicker = " << (effects.value("light_flicker").toBool() ? "true" : "false") << "\n"
           << "            custom bool application:motionBlur = " << (effects.value("train_motion_blur").toBool() ? "true" : "false") << "\n"
           << "        }\n"
           << "    }\n"
           << "}\n";
    stream.flush();
    if (!stage.commit()) {
        result.error = QString("Unable to commit OpenUSD stage: %1").arg(outputPath);
        return result;
    }

    const QString usdcat = QStandardPaths::findExecutable("usdcat");
    if (!usdcat.isEmpty()) {
        QProcess validator;
        validator.start(usdcat, {"--loadOnly", outputPath});
        if (!validator.waitForFinished(10000) || validator.exitCode() != 0) {
            result.error = QString("OpenUSD rejected generated stage: %1")
                               .arg(QString::fromUtf8(validator.readAllStandardError()).trimmed());
            return result;
        }
    }

    result.success = true;
    result.stagePath = outputPath;
    return result;
}

ShotEngine::UsdRenderResult ShotEngine::renderOpenUsdStage(const QString& stagePath, const QString& outputPattern,
                                                           int startFrame, int endFrame, int width,
                                                           const QString& cameraPath) {
    UsdRenderResult result;
    const QString validationError = validateAuthoredUsdStage(stagePath);
    if (!validationError.isEmpty()) {
        result.error = validationError;
        return result;
    }
    if (!QFileInfo::exists(stagePath)) {
        result.error = QString("OpenUSD stage does not exist: %1").arg(stagePath);
        return result;
    }
    if (!outputPattern.contains('#')) {
        result.error = "OpenUSD frame output must include a frame placeholder such as frame.###.png";
        return result;
    }
    if (startFrame < 0 || endFrame < startFrame) {
        result.error = "OpenUSD render frame range is invalid";
        return result;
    }
    if (endFrame - startFrame > 72000) {
        result.error = "OpenUSD render exceeds the 72,001-frame safety limit";
        return result;
    }
    if (width < 64 || width > 8192) {
        result.error = "OpenUSD render width must be between 64 and 8192";
        return result;
    }
    const QFileInfo outputInfo(outputPattern);
    if (!QDir().mkpath(outputInfo.absolutePath())) {
        result.error = QString("Unable to create OpenUSD frame directory: %1").arg(outputInfo.absolutePath());
        return result;
    }

    const QString usdrecord = QStandardPaths::findExecutable("usdrecord");
    if (usdrecord.isEmpty()) {
        result.error = "usdrecord is not installed; a Hydra USD renderer is required";
        return result;
    }
    // Some usdrecord builds serialize an integer-only hash pattern such as
    // "frame.###.png" in scientific notation after frame 9.  The documented
    // decimal form keeps the frame values distinct.  Normalize the resulting
    // files back to the caller's conventional integer pattern below.
    const int hashesStart = outputPattern.indexOf('#');
    int hashesLength = 0;
    while (hashesStart + hashesLength < outputPattern.size() && outputPattern.at(hashesStart + hashesLength) == '#') {
        ++hashesLength;
    }
    QString rendererPattern = outputPattern;
    rendererPattern.replace(hashesStart, hashesLength,
                            QString(hashesLength, '#') + "." + QString(hashesLength, '#'));

    const QString resolvedCameraPath = cameraPath.isEmpty() ? inspectUsdStage(stagePath).cameraPath : cameraPath;
    QProcess renderer;
    renderer.setProcessChannelMode(QProcess::MergedChannels);
    renderer.start(usdrecord, {
        stagePath, rendererPattern,
        "--frames", QString("%1:%2").arg(startFrame).arg(endFrame),
        "--cam", resolvedCameraPath,
        "--purposes", "guide",
        "--imageWidth", QString::number(width),
        "--enableDomeLightVisibility"
    });
    if (!renderer.waitForStarted(10000)) {
        result.error = "Unable to start usdrecord";
        return result;
    }
    if (!renderer.waitForFinished(10 * 60 * 1000) || renderer.exitCode() != 0) {
        result.error = QString("Hydra Storm render failed: %1")
                           .arg(QString::fromUtf8(renderer.readAll()).trimmed());
        return result;
    }

    for (int frame = startFrame; frame <= endFrame; ++frame) {
        QString renderedPath = rendererPattern;
        renderedPath.replace(hashesStart, hashesLength * 2 + 1,
                             QString("%1").arg(frame, hashesLength * 2 + 1, 10, QChar('0')));
        if (!QFileInfo::exists(renderedPath)) {
            result.error = QString("Hydra Storm did not produce frame %1").arg(renderedPath);
            return result;
        }
        QString framePath = outputPattern;
        framePath.replace(hashesStart, hashesLength, QString("%1").arg(frame, hashesLength, 10, QChar('0')));
        if (QFileInfo::exists(framePath) && !QFile::remove(framePath)) {
            result.error = QString("Unable to replace OpenUSD frame %1").arg(framePath);
            return result;
        }
        if (!QFile::rename(renderedPath, framePath)) {
            result.error = QString("Unable to normalize OpenUSD frame name %1").arg(framePath);
            return result;
        }
        result.frames.append(framePath);
    }
    result.success = true;
    return result;
}

ShotEngine::VideoResult ShotEngine::encodeFrameSequence(const QString& inputPattern, int fps, const QString& moviePath) {
    VideoResult result;
    const QString ffmpegInput = ffmpegPattern(inputPattern);
    if (ffmpegInput.isEmpty()) {
        result.error = "Video input must include a frame placeholder such as frame.###.png";
        return result;
    }
    if (fps <= 0 || fps > 120) {
        result.error = "Video frame rate must be between 1 and 120";
        return result;
    }
    if (moviePath.isEmpty() || !moviePath.endsWith(".mp4", Qt::CaseInsensitive)) {
        result.error = "Video output must be an .mp4 file";
        return result;
    }
    if (!QDir().mkpath(QFileInfo(moviePath).absolutePath())) {
        result.error = QString("Unable to create video directory: %1").arg(QFileInfo(moviePath).absolutePath());
        return result;
    }
    const QString ffmpeg = QStandardPaths::findExecutable("ffmpeg");
    if (ffmpeg.isEmpty()) {
        result.error = "ffmpeg is not installed; install it to encode MP4 output";
        return result;
    }
    QProcess encoder;
    encoder.setProcessChannelMode(QProcess::MergedChannels);
    encoder.start(ffmpeg, {
        "-y", "-framerate", QString::number(fps), "-start_number", "0", "-i", ffmpegInput,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", moviePath
    });
    if (!encoder.waitForStarted(10000)) {
        result.error = "Unable to start ffmpeg";
        return result;
    }
    if (!encoder.waitForFinished(10 * 60 * 1000) || encoder.exitCode() != 0 || !QFileInfo::exists(moviePath)) {
        result.error = QString("MP4 encoding failed: %1")
                           .arg(QString::fromUtf8(encoder.readAll()).trimmed());
        return result;
    }
    result.success = true;
    result.moviePath = moviePath;
    return result;
}


