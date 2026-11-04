#include <QApplication>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTimer>
#include <QDir>
#include <QEventLoop>
#include <QMouseEvent>
#include <QStackedWidget>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QProcess>
#include <QSaveFile>
#include <QStandardPaths>
#include <QUrl>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonValue>
#include <QRegularExpression>
#include <QDateTime>
#include <QTextStream>
#include <QTemporaryDir>
#include "MainWindow.h"
#include "ShotEngine.h"
#include "UsdViewportWidget.h"
#include "ActionTimelineWidget.h"

namespace {
constexpr auto kPromptVersion = "2.0";

QByteArray defaultSceneLayerSource() {
    return "#usda 1.0\n"
           "(\n"
           "    defaultPrim = \"World\"\n"
           "    upAxis = \"Y\"\n"
           "    metersPerUnit = 1\n"
           ")\n\n"
           "def Xform \"World\"\n"
           "{\n"
           "    def DomeLight \"BaseAmbient\"\n"
           "    {\n"
           "        color3f inputs:color = (0.35, 0.42, 0.55)\n"
           "        float inputs:intensity = 0.4\n"
           "    }\n"
           "    def DistantLight \"BaseKeyLight\"\n"
           "    {\n"
           "        color3f inputs:color = (0.95, 0.88, 0.78)\n"
           "        float inputs:intensity = 1.6\n"
           "        float angle = 0.53\n"
           "        float3 xformOp:rotateXYZ = (-35, -25, 0)\n"
           "        uniform token[] xformOpOrder = [\"xformOp:rotateXYZ\"]\n"
           "    }\n"
           "    def Xform \"Ground\"\n"
           "    {\n"
           "        double3 xformOp:translate = (0, -0.04, 0)\n"
           "        float3 xformOp:scale = (24, 0.08, 24)\n"
           "        uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:scale\"]\n"
           "        def Cube \"Floor\"\n"
           "        {\n"
           "            float size = 1\n"
           "            color3f[] primvars:displayColor = [(0.16, 0.20, 0.24)] (\n"
           "                interpolation = \"constant\"\n"
           "            )\n"
           "        }\n"
           "    }\n"
           "}\n";
}

int parseShotOrder(const QString& shotFolderName) {
    const auto match = QRegularExpression("(\\d+)$").match(shotFolderName);
    return match.hasMatch() ? match.captured(1).toInt() : 1;
}

// ---------------------------------------------------------------------------
// World state record (tools/world_state.py). USD stays the source of truth; the
// record is a generated summary rebuilt before every LLM request.

QString locateRepoFile(const QString& projectRoot, const QString& relativePath) {
    QStringList roots{projectRoot};
    QDir appDir(QCoreApplication::applicationDirPath());
    do {
        roots << appDir.absolutePath();
    } while (appDir.cdUp());
    for (const QString& root : roots) {
        const QString candidate = QDir(root).filePath(relativePath);
        if (QFileInfo::exists(candidate)) return candidate;
    }
    return {};
}

QString worldStatePython(const QString& projectRoot) {
    const QString configured = qEnvironmentVariable("PAINTERSPACE_PYTHON");
    if (!configured.isEmpty()) return configured;
    const QString venv = locateRepoFile(projectRoot, ".venv/bin/python3");
    if (!venv.isEmpty()) return venv;
    return QStandardPaths::findExecutable("python3");
}

struct WorldStateRun {
    bool ok = false;
    QString error;
    QJsonObject result;
};

WorldStateRun runWorldStateTool(const QString& command, const QString& projectRoot, const QString& shotStagePath) {
    WorldStateRun run;
    const QString script = locateRepoFile(projectRoot, "tools/world_state.py");
    const QString python = worldStatePython(projectRoot);
    if (script.isEmpty() || python.isEmpty()) {
        run.error = "tools/world_state.py or a Python interpreter was not found";
        return run;
    }
    QStringList arguments{script, command, shotStagePath};
    if (qEnvironmentVariable("PAINTERSPACE_SEND_IMAGES") == "0") arguments << "--no-images";
    QProcess process;
    process.setWorkingDirectory(projectRoot);
    process.start(python, arguments);
    if (!process.waitForStarted(10000) || !process.waitForFinished(5 * 60 * 1000)) {
        run.error = "world_state.py did not finish";
        process.kill();
        return run;
    }
    const QByteArray out = process.readAllStandardOutput().trimmed();
    const QByteArray err = process.readAllStandardError().trimmed();
    if (process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0) {
        run.error = QString::fromUtf8(err.isEmpty() ? out : err);
        return run;
    }
    const QList<QByteArray> lines = out.split('\n');
    run.result = QJsonDocument::fromJson(lines.isEmpty() ? QByteArray() : lines.last()).object();
    run.ok = !run.result.isEmpty();
    if (!run.ok) run.error = "world_state.py returned no result";
    if (!err.isEmpty()) qWarning().noquote() << QString::fromUtf8(err);
    return run;
}

QJsonObject imagePart(const QString& path) {
    QFile file(path);
    if (path.isEmpty() || !file.open(QIODevice::ReadOnly)) return {};
    return QJsonObject{
        {"type", "image_url"},
        {"image_url", QJsonObject{
            {"url", "data:image/png;base64," + QString::fromLatin1(file.readAll().toBase64())},
            {"detail", "high"}
        }}
    };
}

const char* kWorldStateInstructions =
    "WORLD STATE RECORD: the user message includes a JSON record of what is in the scene right now, computed by code "
    "from the real USD stage. Trust its numbers instead of estimating geometry yourself. Conventions: meters, Y up, "
    "north = -Z, east = +X, ground at world.ground_y. position is the centre of an object's base. heading_deg is the "
    "facing direction seen from above: 0 = north (-Z), 90 = east (+X), 180 = south, 270 = west, clockwise. "
    "All normalized assets in the manifest (forward_axis = \"-Z\", including *.norm.usda wrappers for characters, cars, and trees) "
    "face -Z with base at y = 0; author xformOp:rotateY = -heading_deg. Only if a legacy un-normalized asset has forward_axis = \"+Z\", "
    "author xformOp:rotateY = 180 - heading_deg. "
    "SOLID OBJECTS & COLLISION PREVENTION: Characters, cars, doors, and tree trunks are solid 3D colliders validated at every frame. "
    "Each object in state_start.json includes world-space `anchors` (e.g. `door_fl_stand`, `stand_front`, `stand_left`, `stand_right`) "
    "that are guaranteed clear of solid colliders. To make a character walk/run to an object or position around obstacles automatically, "
    "set navigation intent in the character prim's customData, e.g. `customData = { string asset_id = \"character.human\", string go_to = \"Car:door_fl_stand\" }` "
    "(or `double2 go_to = (x, z)`), and if the shot requires touching a door or object add `string allow_contact = \"Car:door_fl\"`. "
    "If continuity.from names a previous shot, every object with defined_in = \"shot\" must be re-created in this shot at its listed position, "
    "heading and pose, because shots only sublayer scene.usda. On every prim that references a manifest asset, add "
    "customData = { string asset_id = \"<manifest id>\" } so the next world state record can identify it. "
    "Images, when attached: a labelled top-down map (north up) and the camera's current view.\n";

QByteArray compactManifestForPrompt(const QByteArray& rawManifest) {
    QJsonParseError err;
    QJsonDocument doc = QJsonDocument::fromJson(rawManifest, &err);
    if (err.error != QJsonParseError::NoError || !doc.isObject()) return rawManifest;
    QJsonObject root = doc.object();
    QJsonArray assets = root.value("assets").toArray();
    QJsonArray compactAssets;
    for (const QJsonValue& val : assets) {
        if (!val.isObject()) continue;
        QJsonObject item = val.toObject();
        item.remove("collision");
        item.remove("normalization");
        if (item.value("parts").isObject()) {
            QJsonObject parts = item.value("parts").toObject();
            for (const QString& key : parts.keys()) {
                if (parts.value(key).isObject()) {
                    QJsonObject partObj = parts.value(key).toObject();
                    partObj.remove("collider_frame");
                    parts[key] = partObj;
                }
            }
            item["parts"] = parts;
        }
        compactAssets.append(item);
    }
    root["assets"] = compactAssets;
    return QJsonDocument(root).toJson(QJsonDocument::Indented);
}

bool buildSceneWorldState(const QString& projectRoot, const QString& sceneStagePath, QString* outWorldStateText = nullptr, QStringList* outImages = nullptr) {
    const QString script = locateRepoFile(projectRoot, "tools/world_state.py");
    const QString python = worldStatePython(projectRoot);
    if (script.isEmpty() || python.isEmpty() || !QFileInfo::exists(sceneStagePath)) {
        return false;
    }
    const QFileInfo sceneInfo(sceneStagePath);
    const QString sceneDir = sceneInfo.absolutePath();
    const QString sceneId = sceneInfo.dir().dirName();
    const QString outJsonPath = QDir(sceneDir).filePath("world_state.json");
    const QString outTopdownPath = QDir(sceneDir).filePath("world_state_topdown.png");

    QStringList arguments{script, QStringLiteral("build"), sceneStagePath,
                          QStringLiteral("--frame"), QStringLiteral("start"),
                          QStringLiteral("--output"), outJsonPath,
                          QStringLiteral("--label"), sceneId + QStringLiteral(" baseline")};
    if (qEnvironmentVariable("PAINTERSPACE_SEND_IMAGES") != "0") {
        arguments << QStringLiteral("--topdown") << outTopdownPath;
    }
    QProcess process;
    process.setWorkingDirectory(projectRoot);
    process.start(python, arguments);
    if (!process.waitForStarted(10000) || !process.waitForFinished(5 * 60 * 1000) ||
        process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0) {
        return false;
    }
    if (outWorldStateText) {
        QFile stateFile(outJsonPath);
        if (stateFile.open(QIODevice::ReadOnly)) {
            *outWorldStateText = QString::fromUtf8(stateFile.readAll());
        }
    }
    if (outImages && QFileInfo::exists(outTopdownPath) && qEnvironmentVariable("PAINTERSPACE_SEND_IMAGES") != "0") {
        outImages->append(outTopdownPath);
    }
    return true;
}

bool writeSceneMetadata(const QString& stagePath, const QString& prompt, const QString& modelName, bool validationPassed) {
    const QFileInfo stageInfo(stagePath);
    const QString sceneId = stageInfo.dir().dirName();
    const QString metadataPath = stageInfo.dir().filePath("scene.json");

    QString originalPrompt = prompt;
    QJsonArray revisions;
    QFile existingMetaFile(metadataPath);
    if (existingMetaFile.open(QIODevice::ReadOnly)) {
        const QJsonObject existing = QJsonDocument::fromJson(existingMetaFile.readAll()).object();
        const QString prevPrompt = existing.value("prompt").toString().trimmed();
        revisions = existing.value("revisions").toArray();
        if (!prevPrompt.isEmpty()) {
            originalPrompt = prevPrompt;
            if (prompt.trimmed() != prevPrompt) {
                revisions.append(prompt.trimmed());
            }
        }
    }

    const QJsonObject metadata{
        {"scene_id", sceneId},
        {"stage", stageInfo.fileName()},
        {"prompt", originalPrompt},
        {"revisions", revisions},
        {"configured", true},
        {"llm_model", modelName},
        {"prompt_version", QString::fromLatin1(kPromptVersion)},
        {"validation_passed", validationPassed}
    };

    QSaveFile metaOut(metadataPath);
    const QByteArray bytes = QJsonDocument(metadata).toJson(QJsonDocument::Indented);
    return metaOut.open(QIODevice::WriteOnly | QIODevice::Text) &&
           metaOut.write(bytes) == bytes.size() &&
           metaOut.commit();
}

bool writeShotMetadata(const QString& stagePath, const QString& prompt, const QString& modelName, bool validationPassed) {
    const QFileInfo stageInfo(stagePath);
    const QString shotDir = stageInfo.absolutePath();
    const QString shotId = stageInfo.dir().dirName();
    const QString sceneId = QDir(shotDir + "/..").dirName();
    const QString metadataPath = stageInfo.dir().filePath("shot.json");

    QString originalPrompt = prompt;
    QJsonArray revisions;
    QFile existingMetaFile(metadataPath);
    if (existingMetaFile.open(QIODevice::ReadOnly)) {
        const QJsonObject existing = QJsonDocument::fromJson(existingMetaFile.readAll()).object();
        const QString prevPrompt = existing.value("prompt").toString().trimmed();
        revisions = existing.value("revisions").toArray();
        if (!prevPrompt.isEmpty()) {
            originalPrompt = prevPrompt;
            if (prompt.trimmed() != prevPrompt) {
                revisions.append(prompt.trimmed());
            }
        }
    }

    const ShotEngine::UsdStageInfo inspected = ShotEngine::inspectUsdStage(stagePath);
    const QJsonObject metadata{
        {"shot_id", shotId},
        {"scene_id", sceneId},
        {"scene_order", parseShotOrder(shotId)},
        {"stage", stageInfo.fileName()},
        {"prompt", originalPrompt},
        {"revisions", revisions},
        {"fps", inspected.fps},
        {"frame_range", QJsonArray{inspected.startFrame, inspected.endFrame}},
        {"start_frame", inspected.startFrame},
        {"end_frame", inspected.endFrame},
        {"duration", inspected.duration},
        {"camera_prim", inspected.cameraPath},
        {"llm_model", modelName},
        {"prompt_version", QString::fromLatin1(kPromptVersion)},
        {"validation_passed", validationPassed}
    };

    QSaveFile metaOut(metadataPath);
    const QByteArray bytes = QJsonDocument(metadata).toJson(QJsonDocument::Indented);
    return metaOut.open(QIODevice::WriteOnly | QIODevice::Text) &&
           metaOut.write(bytes) == bytes.size() &&
           metaOut.commit();
}

bool writeAgentTaskQueue(const QString& projectRootPath,
                         const QFileInfo& outputInfo,
                         bool isSceneStage,
                         const QString& prompt,
                         const QString& sceneDescriptionPrompt,
                         const QString& existingSceneLayerText,
                         const QString& existingShotLayerText,
                         const QString& worldStateText,
                         const QStringList& worldStateImages,
                         const QByteArray& manifestBytes,
                         bool cameraLocked = false) {
    const QDir projectRootDir(projectRootPath);
    const QDir queueDir(projectRootDir.filePath("task_queue"));
    queueDir.mkpath(".");

    // Remove any stale LLM response or validation error from a previous run
    QFile::remove(queueDir.filePath("llm_response.json"));
    QFile::remove(queueDir.filePath("validation_error.json"));

    const QString taskId = QString("%1_%2")
        .arg(outputInfo.dir().dirName())
        .arg(QDateTime::currentDateTime().toString("yyyyMMdd_hhmmss"));

    QJsonObject task;
    task["task_id"] = taskId;
    task["status"] = QStringLiteral("pending");
    task["provider"] = QStringLiteral("ide_chat_llm_endpoint");
    task["llm_endpoint_mode"] = true;
    task["response_file"] = QStringLiteral("task_queue/llm_response.json");
    task["stage_type"] = isSceneStage ? QStringLiteral("scene") : QStringLiteral("shot");
    task["target_stage"] = projectRootDir.relativeFilePath(outputInfo.absoluteFilePath());
    task["prompt"] = prompt;
    if (!sceneDescriptionPrompt.isEmpty()) {
        task["scene_prompt"] = sceneDescriptionPrompt;
    }
    task["scene_stage"] = isSceneStage
        ? projectRootDir.relativeFilePath(outputInfo.absoluteFilePath())
        : projectRootDir.relativeFilePath(outputInfo.dir().filePath("../scene.usda"));
    task["has_existing_stage"] = !existingShotLayerText.isEmpty() || !existingSceneLayerText.isEmpty();
    if (!existingSceneLayerText.isEmpty()) {
        task["existing_scene_usda"] = existingSceneLayerText;
    }
    if (!existingShotLayerText.isEmpty()) {
        task["existing_shot_usda"] = existingShotLayerText;
    }
    if (!worldStateText.isEmpty()) {
        task["world_state"] = QJsonDocument::fromJson(worldStateText.toUtf8()).object();
    }
    QJsonArray imageList;
    for (const QString& img : worldStateImages) {
        imageList.append(projectRootDir.relativeFilePath(img));
    }
    task["world_state_images"] = imageList;
    task["manifest_summary"] = QJsonDocument::fromJson(manifestBytes).object();
    task["instructions"] = QStringLiteral(
        "CRITICAL INSTRUCTION FOR IDE CHATBOT / LLM: You are acting STRICTLY as the LLM API endpoint. "
        "The LLM must respond ONLY with the tools call JSON ({\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}), "
        "write that JSON to task_queue/llm_response.json, display that JSON in chat, and END EXECUTION IMMEDIATELY THERE. "
        "Use the prompt when present, combine it with relevant drawn routes and explicit timeline segments, "
        "and use timeline segments alone when the prompt is empty. An empty timeline adds no constraint. "
        "DO NOT run compile_shot.py, DO NOT execute any Python/shell scripts, and DO NOT author or edit .usda files directly. "
        "The PainterSpace C++ desktop application watches task_queue/llm_response.json and will execute the function calls, "
        "resolve semantic coordinates, compile the USD stage, and run collision validation itself.");
    task["llm_call"] = QStringLiteral(
        "Respond ONLY with the tools call JSON matching schema: {\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}. "
        "Write the JSON to task_queue/llm_response.json and END EXECUTION THERE. "
        "Do not run the queue processor, planner, compiler, validator, renderer, or shell commands.");
    if (cameraLocked) {
        task["camera_locked"] = true;
    }

    // Write task_queue/pending_request.json
    QFile taskFile(queueDir.filePath("pending_request.json"));
    if (!taskFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
        qCritical("Unable to write task_queue/pending_request.json");
        return false;
    }
    taskFile.write(QJsonDocument(task).toJson(QJsonDocument::Indented));
    taskFile.close();

    // Enrich pending_request.json with the 76-function FunctionCatalog schemas
    {
        const QString prepScript = projectRootDir.filePath("tools/process_agent_queue.py");
        const QString py = worldStatePython(projectRootPath);
        if (!QFileInfo::exists(prepScript) || py.isEmpty()) {
            qCritical("Queue preparation unavailable; request was not sent");
            QFile::remove(queueDir.filePath("pending_request.json"));
            return false;
        }
        QProcess prepProc;
        prepProc.setWorkingDirectory(projectRootPath);
        prepProc.start(py, {prepScript, QStringLiteral("prepare")});
        const bool prepared = prepProc.waitForFinished(30000);
        if (!prepared) {
            prepProc.kill();
            prepProc.waitForFinished(1000);
        }
        if (!prepared || prepProc.exitStatus() != QProcess::NormalExit || prepProc.exitCode() != 0) {
            qCritical().noquote() << "Queue preparation failed:" << QString::fromUtf8(prepProc.readAllStandardError());
            QFile::remove(queueDir.filePath("pending_request.json"));
            return false;
        }
        QFile preparedFile(queueDir.filePath("pending_request.json"));
        if (!preparedFile.open(QIODevice::ReadOnly)) {
            qCritical("Prepared shot request cannot be read");
            return false;
        }
        const QJsonObject preparedTask = QJsonDocument::fromJson(preparedFile.readAll()).object();
        if (!preparedTask.contains("scene_context") || preparedTask.value("planner_rules").toString().isEmpty()) {
            qCritical("Prepared shot request is missing current scene context");
            preparedFile.close();
            QFile::remove(queueDir.filePath("pending_request.json"));
            return false;
        }
    }

    // Also write a human-readable markdown summary with explicit LLM-only rules
    QFile mdFile(queueDir.filePath("pending_request.md"));
    if (mdFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
        QTextStream ts(&mdFile);
        ts << "# Pending PainterSpace LLM Endpoint Request\n\n";
        ts << "- **Task ID**: `" << taskId << "`\n";
        ts << "- **Type**: " << (isSceneStage ? "Scene Layer" : "Shot Layer") << "\n";
        ts << "- **Target Stage**: `" << task["target_stage"].toString() << "`\n";
        ts << "- **Response File**: `task_queue/llm_response.json`\n";
        ts << "- **Prompt**: \"" << prompt << "\"\n";
        ts << "\n";
        ts << "## STRICT PROTOCOL FOR IDE CHATBOT / LLM\n";
        ts << "1. **Act strictly as the LLM API endpoint.**\n";
        ts << "2. Read `task_queue/pending_request.json` (which includes `function_catalog_schemas` and `director_brief_digest`).\n";
        ts << "3. The LLM must respond **ONLY with the tools call JSON**, and execution **MUST END THERE**.\n";
        ts << "4. Write **ONLY** the tools call JSON response to `task_queue/llm_response.json`:\n";
        ts << "```json\n{\n  \"calls\": [\n    {\"function\": \"...\", \"args\": {...}}\n  ]\n}\n```\n";
        ts << "5. Output that JSON in chat and **STOP IMMEDIATELY — END EXECUTION**.\n";
        ts << "6. **NEVER** run `compile_shot.py`, Python scripts, shell commands, or write `.usda` files directly. "
           << "The running `PainterSpace` C++ application watches `task_queue/llm_response.json` and executes the function calls itself.\n";
        mdFile.close();
    }

    // Compiler metadata remains authoritative until a new generation succeeds.

    qInfo().noquote() << QStringLiteral("[AGENT_DISPATCH] Task queued in task_queue/pending_request.json for target: %1")
                            .arg(task["target_stage"].toString());
    qInfo("Waiting for IDE LLM to write JSON tool calls to task_queue/llm_response.json...");
    return true;
}

int generateUsda(const QString& prompt, const QString& outputPath, bool cameraLocked = false) {
    const bool agentMode = qgetenv("OPENAI_API_KEY").isEmpty() ||
                           qEnvironmentVariable("PAINTERSPACE_LLM_PROVIDER") == "agent" ||
                           qEnvironmentVariable("PAINTERSPACE_AGENT_MODE") == "1";
    const QByteArray apiKey = qgetenv("OPENAI_API_KEY");
    if (!agentMode && apiKey.isEmpty()) {
        qCritical("Set OPENAI_API_KEY in the environment or run in agent mode to use --generate-usda");
        return 2;
    }
    if (outputPath.split('/').contains("..")) {
        qCritical("Generated stage path cannot contain parent traversal");
        return 2;
    }
    const QFileInfo outputInfo(QDir::cleanPath(outputPath));
    const QStringList pathParts = outputInfo.absoluteFilePath().split('/', Qt::SkipEmptyParts);
    const int n = pathParts.size();
    const bool isSceneStage = (n >= 3 && pathParts.at(n - 3) == "scenes" &&
                               pathParts.at(n - 2).startsWith("scene_") &&
                               pathParts.at(n - 1) == "scene.usda");
    const bool isShotStage = (n >= 4 && pathParts.at(n - 4) == "scenes" &&
                              pathParts.at(n - 3).startsWith("scene_") &&
                              pathParts.at(n - 2).startsWith("shot_") &&
                              pathParts.at(n - 1) == "shot.usda");
    if (!isSceneStage && !isShotStage) {
        qCritical("Generated stages must be stored as scenes/scene_XX/scene.usda or scenes/scene_XX/shot_XXX/shot.usda");
        return 2;
    }
    if (!QDir().mkpath(outputInfo.absolutePath())) {
        qCritical("Unable to create stage output folder");
        return 1;
    }
    const QDir projectRootDir(QDir::cleanPath(outputInfo.absolutePath() + (isSceneStage ? "/../.." : "/../../..")));
    const QString projectRootPath = projectRootDir.absolutePath();
    {
        const QString syncScript = projectRootDir.filePath("tools/sync_manifest.py");
        const QString py = worldStatePython(projectRootPath);
        if (QFileInfo::exists(syncScript) && !py.isEmpty()) {
            QProcess syncProc;
            syncProc.setWorkingDirectory(projectRootPath);
            syncProc.start(py, {syncScript, QStringLiteral("--project-root"), projectRootPath, QStringLiteral("--quiet")});
            syncProc.waitForFinished(15000);
        }
    }
    QFile manifest(projectRootDir.filePath("manifest.json"));
    if (!manifest.exists()) {
        manifest.setFileName(projectRootDir.filePath("assets/manifest.json"));
    }
    if (!manifest.open(QIODevice::ReadOnly)) {
        qCritical("Unable to read manifest.json or assets/manifest.json");
        return 1;
    }

    const QByteArray manifestBytes = compactManifestForPrompt(manifest.readAll());
    const QString modelName = agentMode ? QStringLiteral("gemini-coding-agent") : qEnvironmentVariable("PAINTERSPACE_OPENAI_MODEL", "gpt-4.1-mini");

    if (isSceneStage) {
        QString existingSceneLayerText;
        QFile existingSceneFile(outputInfo.absoluteFilePath());
        if (existingSceneFile.open(QIODevice::ReadOnly)) {
            existingSceneLayerText = QString::fromUtf8(existingSceneFile.readAll());
        }

        QString worldStateText;
        QStringList worldStateImages;
        buildSceneWorldState(projectRootPath, outputInfo.absoluteFilePath(), &worldStateText, &worldStateImages);

        const QString systemPrompt =
            "You are a film production designer authoring an OpenUSD ASCII SCENE LAYER (scene.usda). Return only valid USDA text beginning #usda 1.0; never return markdown, code fences, URLs, scripts, or Python. "
            "This layer must set defaultPrim = \"World\", upAxis = \"Y\", and metersPerUnit = 1, and define `def Xform \"World\"` containing the shared environment, buildings, static props, and base lighting (such as DomeLight and DistantLight). "
            "Do not use subLayers in scene.usda. "
            "Use only USD references from the manifest that have usd_reference_ready=true; because scene.usda lives at scenes/scene_XX/scene.usda, asset references MUST start with ../../assets/ (for example @../../assets/usdc_assets/environments/downtown_art_deco_tower.usdc@</root> or @../../assets/usdc_assets/humans/human.norm.usda@</root>). "
            "On every prim that references a manifest asset, add customData = { string asset_id = \"<manifest id>\" }. "
            "Keep stages below 1000 prims.\n" +
            (worldStateText.isEmpty() ? QString() : QString::fromLatin1(kWorldStateInstructions)) +
            "Asset manifest:\n" + QString::fromUtf8(manifestBytes);

        QString userContent = "Scene description / prompt:\n" + prompt;
        if (!existingSceneLayerText.isEmpty()) {
            userContent = "Current scene.usda (from 3D editor or previous scene setup):\n" + existingSceneLayerText + "\n\n" + userContent;
        }
        if (!worldStateText.isEmpty()) {
            userContent = "WORLD STATE RECORD (world_state.json, what is in the 3D scene right now):\n" + worldStateText + "\n\n" + userContent;
        }
        QJsonValue userMessageContent = userContent;
        if (!worldStateImages.isEmpty()) {
            QJsonArray parts{QJsonObject{{"type", "text"}, {"text", userContent}}};
            for (const QString& imagePath : worldStateImages) {
                const QJsonObject part = imagePart(imagePath);
                if (part.isEmpty()) continue;
                parts.append(QJsonObject{{"type", "text"}, {"text", QStringLiteral("Top-down map of the current scene world state (north up, object ids labelled):")}});
                parts.append(part);
            }
            userMessageContent = parts;
        }

        if (agentMode) {
            return writeAgentTaskQueue(projectRootPath, outputInfo, true, prompt, QString(),
                                       existingSceneLayerText, QString(), worldStateText,
                                       worldStateImages, manifestBytes) ? 0 : 1;
        }

        const QJsonObject body{
            {"model", modelName},
            {"messages", QJsonArray{
                QJsonObject{{"role", "system"}, {"content", systemPrompt}},
                QJsonObject{{"role", "user"}, {"content", userMessageContent}}
            }},
            {"temperature", 0.7}
        };
        QNetworkAccessManager manager;
        QNetworkRequest request(QUrl("https://api.openai.com/v1/chat/completions"));
        request.setHeader(QNetworkRequest::ContentTypeHeader, "application/json");
        request.setRawHeader("Authorization", "Bearer " + apiKey);
        QNetworkReply* reply = manager.post(request, QJsonDocument(body).toJson(QJsonDocument::Compact));
        QEventLoop loop;
        QTimer timeout;
        timeout.setSingleShot(true);
        QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
        QObject::connect(&timeout, &QTimer::timeout, &loop, &QEventLoop::quit);
        timeout.start(120000);
        loop.exec();
        if (!reply->isFinished()) {
            reply->abort();
            qCritical("USD scene generation request timed out");
            reply->deleteLater();
            return 1;
        }
        const QByteArray response = reply->readAll();
        if (reply->error() != QNetworkReply::NoError) {
            qCritical().noquote() << "USD scene generation request failed:" << reply->errorString() << response;
            reply->deleteLater();
            return 1;
        }
        reply->deleteLater();
        QJsonParseError parseError;
        const QJsonDocument responseDoc = QJsonDocument::fromJson(response, &parseError);
        QString stageText = responseDoc.object().value("choices").toArray().at(0).toObject()
                                .value("message").toObject().value("content").toString().trimmed();
        stageText.remove(QRegularExpression("^```(?:usda)?\\s*", QRegularExpression::CaseInsensitiveOption));
        stageText.remove(QRegularExpression("\\s*```$"));
        stageText.replace(QStringLiteral("@../../../assets/"), QStringLiteral("@../../assets/"));
        stageText = stageText.trimmed();
        if (parseError.error != QJsonParseError::NoError || !stageText.startsWith("#usda 1.0")) {
            qCritical("Model response did not contain a USDA scene stage");
            return 1;
        }
        QSaveFile output(outputInfo.absoluteFilePath());
        const QByteArray stageBytes = (stageText + "\n").toUtf8();
        if (!output.open(QIODevice::WriteOnly | QIODevice::Text) || output.write(stageBytes) != stageBytes.size() || !output.commit()) {
            qCritical("Unable to save generated USDA scene stage");
            return 1;
        }
        const QString validation = ShotEngine::validateAuthoredUsdStage(output.fileName());
        writeSceneMetadata(output.fileName(), prompt, modelName, validation.isEmpty());
        if (!validation.isEmpty()) {
            qCritical().noquote() << "Generated scene stage failed validation:" << validation;
            return 1;
        }
        buildSceneWorldState(projectRootPath, output.fileName());
        qInfo().noquote() << "Generated and validated scene" << output.fileName();
        return 0;
    }

    const QString sceneStagePath = QDir(outputInfo.dir().absolutePath()).absoluteFilePath("../scene.usda");
    if (!QFileInfo::exists(sceneStagePath)) {
        QSaveFile sceneStage(sceneStagePath);
        const QByteArray sceneSource = defaultSceneLayerSource();
        if (!sceneStage.open(QIODevice::WriteOnly | QIODevice::Text) ||
            sceneStage.write(sceneSource) != sceneSource.size() || !sceneStage.commit()) {
            qCritical("Unable to create shared scene.usda layer");
            return 1;
        }
    }
    QString existingSceneLayerText;
    QFile sceneLayerFile(sceneStagePath);
    if (sceneLayerFile.open(QIODevice::ReadOnly)) {
        existingSceneLayerText = QString::fromUtf8(sceneLayerFile.readAll());
    }
    QString sceneDescriptionPrompt;
    QFile sceneMetaFile(QDir(outputInfo.dir().absolutePath()).absoluteFilePath("../scene.json"));
    if (sceneMetaFile.open(QIODevice::ReadOnly)) {
        const QJsonObject sceneMeta = QJsonDocument::fromJson(sceneMetaFile.readAll()).object();
        sceneDescriptionPrompt = sceneMeta.value("prompt").toString().trimmed();
        const QJsonArray sceneRevs = sceneMeta.value("revisions").toArray();
        if (!sceneRevs.isEmpty()) {
            sceneDescriptionPrompt = sceneRevs.last().toString(sceneDescriptionPrompt).trimmed();
        }
    }
    QString existingShotLayerText;
    QFile existingShotFile(outputInfo.absoluteFilePath());
    if (existingShotFile.open(QIODevice::ReadOnly)) {
        existingShotLayerText = QString::fromUtf8(existingShotFile.readAll());
    }

    // Rebuild the world state record from the real stage before every request.
    QString worldStateText;
    QStringList worldStateImages;
    const WorldStateRun prepared = runWorldStateTool("prepare-shot", projectRootPath, outputInfo.absoluteFilePath());
    if (prepared.ok) {
        QFile stateFile(prepared.result.value("state").toString());
        if (stateFile.open(QIODevice::ReadOnly)) worldStateText = QString::fromUtf8(stateFile.readAll());
        const QJsonObject images = prepared.result.value("images").toObject();
        for (const QString& key : {QStringLiteral("topdown"), QStringLiteral("camera")}) {
            if (images.value(key).isString()) worldStateImages << images.value(key).toString();
        }
    } else {
        qWarning().noquote() << "World state record unavailable; generating without it:" << prepared.error;
    }

    if (agentMode && qEnvironmentVariable("PAINTERSPACE_OFFLINE_COMPILER") != QStringLiteral("1")) {
        return writeAgentTaskQueue(projectRootPath, outputInfo, false, prompt, sceneDescriptionPrompt,
                                   existingSceneLayerText, existingShotLayerText, worldStateText,
                                   worldStateImages, manifestBytes, cameraLocked) ? 0 : 1;
    }

    // Primary Shot Generation Pipeline: Declarative Shot Function Library Compiler (compile_shot.py)
    if (qEnvironmentVariable("PAINTERSPACE_RAW_LLM") != QStringLiteral("1") &&
        qEnvironmentVariable("PAINTERSPACE_QUEUE_ONLY") != QStringLiteral("1")) {
        const QString compileScript = projectRootDir.filePath("tools/shot_library/compile_shot.py");
        const QString py = worldStatePython(projectRootPath);
        if (QFileInfo::exists(compileScript) && !py.isEmpty()) {
            QProcess compileProc;
            compileProc.setWorkingDirectory(projectRootPath);
            QStringList compileArgs{
                compileScript,
                QStringLiteral("--prompt"), prompt,
                QStringLiteral("--output"), outputInfo.absoluteFilePath(),
                QStringLiteral("--scene"), sceneStagePath
            };
            if (cameraLocked) {
                compileArgs << QStringLiteral("--camera-locked");
            }
            compileProc.start(py, compileArgs);
            if (compileProc.waitForFinished(180000) && compileProc.exitCode() == 0 &&
                QFileInfo::exists(outputInfo.absoluteFilePath())) {
                qInfo().noquote() << "Generated and validated" << outputInfo.absoluteFilePath() << "via ShotCompiler";
                const WorldStateRun finalized = runWorldStateTool("finalize-shot", projectRootPath, outputInfo.absoluteFilePath());
                if (finalized.ok) {
                    qInfo().noquote() << "Wrote" << finalized.result.value("state").toString();
                }
                return 0;
            }
        }
    }

    // A failed symbolic compilation must never fall through to raw USDA generation.
    qCritical("Shot compilation failed; authored director intent has been preserved");
    return 1;


}
}

int main(int argc, char** argv) {
    QApplication app(argc, argv);
    app.setApplicationName("PainterSpace");

    const QStringList arguments = app.arguments();
    const int generateUsdArgument = arguments.indexOf("--generate-usda");
    if (generateUsdArgument >= 0) {
        if (arguments.size() <= generateUsdArgument + 2) {
            qCritical("Usage: PainterSpace --generate-usda <prompt> <scenes/scene_XX/scene.usda|scenes/scene_XX/shot_XXX/shot.usda> [--camera-locked]");
            return 2;
        }
        const bool cameraLocked = arguments.contains("--camera-locked");
        return generateUsda(arguments.at(generateUsdArgument + 1), arguments.at(generateUsdArgument + 2), cameraLocked);
    }
    const int validateUsdArgument = arguments.indexOf("--validate-usda");
    if (validateUsdArgument >= 0) {
        if (arguments.size() <= validateUsdArgument + 1) {
            qCritical("Usage: PainterSpace --validate-usda <stage.usda>");
            return 2;
        }
        const QString error = ShotEngine::validateAuthoredUsdStage(arguments.at(validateUsdArgument + 1));
        if (!error.isEmpty()) {
            qCritical().noquote() << error;
            return 1;
        }
        qInfo() << "Validated OpenUSD ASCII stage";
        return 0;
    }
    const int renderUsdArgument = arguments.indexOf("--render-usd");
    if (renderUsdArgument >= 0) {
        if (arguments.size() <= renderUsdArgument + 3) {
            qCritical("Usage: PainterSpace --render-usd <stage.usda> <frame.###.png> <start:end> [width]");
            return 2;
        }
        const QStringList frameRange = arguments.at(renderUsdArgument + 3).split(':');
        bool startOk = false;
        bool endOk = false;
        const int startFrame = frameRange.value(0).toInt(&startOk);
        const int endFrame = frameRange.value(1).toInt(&endOk);
        bool widthOk = true;
        const int width = arguments.size() > renderUsdArgument + 4
            ? arguments.at(renderUsdArgument + 4).toInt(&widthOk) : 960;
        if (frameRange.size() != 2 || !startOk || !endOk || !widthOk) {
            qCritical("OpenUSD render range must be start:end and width must be numeric");
            return 2;
        }
        const ShotEngine::UsdRenderResult result = ShotEngine::renderOpenUsdStage(
            arguments.at(renderUsdArgument + 1), arguments.at(renderUsdArgument + 2), startFrame, endFrame, width);
        if (!result.success) {
            qCritical().noquote() << result.error;
            return 1;
        }
        qInfo().noquote() << "Rendered" << result.frames.size() << "OpenUSD frame(s)";
        return 0;
    }

    const int encodeArgument = arguments.indexOf("--encode-video");
    if (encodeArgument >= 0) {
        if (arguments.size() <= encodeArgument + 3) {
            qCritical("Usage: PainterSpace --encode-video <frame.###.png> <fps> <movie.mp4>");
            return 2;
        }
        bool fpsOk = false;
        const int fps = arguments.at(encodeArgument + 2).toInt(&fpsOk);
        if (!fpsOk) {
            qCritical("Video frame rate must be numeric");
            return 2;
        }
        const ShotEngine::VideoResult result = ShotEngine::encodeFrameSequence(
            arguments.at(encodeArgument + 1), fps, arguments.at(encodeArgument + 3));
        if (!result.success) {
            qCritical().noquote() << result.error;
            return 1;
        }
        qInfo().noquote() << "Encoded" << result.moviePath;
        return 0;
    }

    const int exportArgument = arguments.indexOf("--export-usd");
    if (exportArgument >= 0) {
        if (arguments.size() <= exportArgument + 2) {
            qCritical("Usage: PainterSpace --export-usd <shot.json> <stage.usda>");
            return 2;
        }
        QFile source(arguments.at(exportArgument + 1));
        if (!source.open(QIODevice::ReadOnly)) {
            qCritical().noquote() << "Unable to read ShotSpec:" << source.fileName();
            return 2;
        }
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(source.readAll(), &parseError);
        if (!document.isObject()) {
            qCritical().noquote() << "Invalid ShotSpec JSON:" << parseError.errorString();
            return 2;
        }
        const ShotEngine::UsdResult result = ShotEngine::buildOpenUsdStage(
            document.object(), arguments.at(exportArgument + 2));
        if (!result.success) {
            qCritical().noquote() << result.error;
            return 1;
        }
        qInfo().noquote() << "Generated OpenUSD stage" << result.stagePath;
        return 0;
    }

    const int renderArgument = arguments.indexOf("--render-shot");
    if (renderArgument >= 0) {
        if (arguments.size() <= renderArgument + 2) {
            qCritical("Usage: PainterSpace --render-shot <shot.json> <output.png>");
            return 2;
        }

        QFile source(arguments.at(renderArgument + 1));
        if (!source.open(QIODevice::ReadOnly)) {
            qCritical().noquote() << "Unable to read ShotSpec:" << source.fileName();
            return 2;
        }
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(source.readAll(), &parseError);
        if (!document.isObject()) {
            qCritical().noquote() << "Invalid ShotSpec JSON:" << parseError.errorString();
            return 2;
        }

        const QString tempUsdPath = QString("/tmp/render_shot_%1.usda").arg(QCoreApplication::applicationPid());
        const ShotEngine::UsdResult buildResult = ShotEngine::buildOpenUsdStage(document.object(), tempUsdPath);
        if (!buildResult.success) {
            qCritical().noquote() << "Failed to build USD stage:" << buildResult.error;
            return 1;
        }

        UsdViewportWidget viewport;
        viewport.resize(1280, 720);
        if (!viewport.openStage(tempUsdPath)) {
            qCritical().noquote() << "Failed to open USD stage in UsdViewportWidget:" << tempUsdPath;
            QFile::remove(tempUsdPath);
            return 1;
        }

        const QImage preview = viewport.renderCurrentView(1280, 720);
        QFile::remove(tempUsdPath);
        if (preview.isNull() || !preview.save(arguments.at(renderArgument + 2))) {
            qCritical().noquote() << "Failed to render preview to" << arguments.at(renderArgument + 2);
            return 1;
        }
        qInfo().noquote() << "Rendered" << arguments.at(renderArgument + 2);
        return 0;
    }

    const int usdSpikeArg = arguments.indexOf("--usd-spike");
    if (usdSpikeArg >= 0) {
        if (arguments.size() <= usdSpikeArg + 1) {
            qCritical("Usage: PainterSpace --usd-spike <stage.usda|asset.usdc> [output.png] [frame]");
            return 2;
        }
        const QString stagePath = arguments.at(usdSpikeArg + 1);
        const QString outputPng = (arguments.size() > usdSpikeArg + 2)
            ? arguments.at(usdSpikeArg + 2)
            : QStringLiteral("/tmp/painterspace_usd_spike.png");
        const double frame = (arguments.size() > usdSpikeArg + 3)
            ? arguments.at(usdSpikeArg + 3).toDouble()
            : 0.0;

        UsdViewportWidget viewport;
        viewport.resize(960, 540);
        if (!viewport.openStage(stagePath)) {
            qCritical().noquote() << "Failed to open USD stage in UsdViewportWidget:" << stagePath;
            return 1;
        }
        viewport.setCurrentFrame(frame);
        const QImage rendered = viewport.renderCurrentView(960, 540);
        if (rendered.isNull() || !rendered.save(outputPng)) {
            qCritical().noquote() << "Failed to render or save USD viewport image:" << outputPng;
            return 1;
        }
        qInfo().noquote() << "USD Hydra Storm viewport rendered" << stagePath
                          << "at frame" << frame << "->" << outputPng
                          << "(" << viewport.primList().size() << "prims, camera:" << viewport.activeCameraPath() << ")";
        return 0;
    }

    const int usdTestSuiteArg = arguments.indexOf("--usd-test-suite");
    if (usdTestSuiteArg >= 0) {
        UsdViewportWidget viewport;
        viewport.resize(960, 540);

        // Phase 0 Exit Test: animated skinned character in UsdViewportWidget with orbit & frame stepping
        QString runningAsset = locateRepoFile(QDir::currentPath(), "assets/usdc_assets/humans/running_fast.usdc");
        if (runningAsset.isEmpty()) {
            runningAsset = locateRepoFile(QDir::currentPath(), "scenes/scene_01/shot_004/shot.usda");
        }
        if (runningAsset.isEmpty()) {
            runningAsset = locateRepoFile(QDir::currentPath(), "scenes/scene_01/shot_001/shot.usda");
        }
        if (runningAsset.isEmpty() || !viewport.openStage(runningAsset)) {
            qCritical("Phase 0 FAIL: Could not open stage for animated character test");
            return 1;
        }
        viewport.setCurrentFrame(0.0);
        const QImage f0 = viewport.renderCurrentView(640, 360);
        viewport.setCurrentFrame(10.0);
        const QImage f10 = viewport.renderCurrentView(640, 360);
        if (f0.isNull() || f10.isNull() || f0 == f10) {
            qCritical("Phase 0 FAIL: Skinned character animation did not advance between frame 0 and frame 10");
            return 1;
        }
        qInfo("Phase 0 PASS: Animated UsdSkel character renders and animates in UsdViewportWidget.");

        // Phase 1 Exit Test: open existing shots and verify Hydra Storm render
        for (const QString& shotRel : {QStringLiteral("scenes/scene_01/shot_001/shot.usda"),
                                       QStringLiteral("scenes/scene_01/shot_002/shot.usda"),
                                       QStringLiteral("scenes/scene_01/shot_003/shot.usda"),
                                       QStringLiteral("scenes/scene_01/shot_004/shot.usda")}) {
            const QString shotPath = locateRepoFile(QDir::currentPath(), shotRel);
            if (shotPath.isEmpty() || !viewport.openStage(shotPath)) {
                qCritical().noquote() << "Phase 1 FAIL: Could not open" << shotRel;
                return 1;
            }
            const QImage img = viewport.renderCurrentView(960, 540);
            if (img.isNull()) {
                qCritical().noquote() << "Phase 1 FAIL: Null render for" << shotRel;
                return 1;
            }
            qInfo().noquote() << "Phase 1 PASS: Rendered" << shotRel << "prims=" << viewport.primList().size()
                              << "camera=" << viewport.activeCameraPath()
                              << "render_ms=" << viewport.lastFrameRenderMs();
        }

        // Phase 2 & Phase 3 Exit Test: move /World/Car in shot_001 via user_edits.usda, verify shot.usda is untouched,
        // reload stage (simulating regeneration), and confirm the manual move is still applied!
        const QString shot1Path = locateRepoFile(QDir::currentPath(), "scenes/scene_01/shot_001/shot.usda");
        QFile shot1FileBefore(shot1Path);
        if (shot1FileBefore.open(QIODevice::ReadOnly)) {
            const QByteArray shot1BytesBefore = shot1FileBefore.readAll();
            shot1FileBefore.close();

            viewport.openStage(shot1Path);
            viewport.clearAllUserEdits();
            viewport.selectPrim("/World/Car");
            const QVector3D origCarPos = viewport.selectedPrimPosition();
            const QVector3D movedCarPos(origCarPos.x() + 3.5f, origCarPos.y(), origCarPos.z() - 1.25f);
            viewport.setSelectedPrimPosition(movedCarPos);

            if (viewport.userEditedPrimCount() < 1) {
                qCritical("Phase 3 FAIL: User edit count did not increment after moving /World/Car");
                return 1;
            }
            // Reload stage (simulating LLM regeneration completing)
            viewport.reloadStage();
            viewport.selectPrim("/World/Car");
            const QVector3D reloadedCarPos = viewport.selectedPrimPosition();
            QFile shot1FileAfter(shot1Path);
            if (shot1FileAfter.open(QIODevice::ReadOnly)) {
                const QByteArray shot1BytesAfter = shot1FileAfter.readAll();
                shot1FileAfter.close();

                if (shot1BytesBefore != shot1BytesAfter) {
                    qCritical("Phase 3 FAIL: shot.usda was modified by manual edit!");
                    return 1;
                }
                if ((reloadedCarPos - movedCarPos).length() > 1e-3f) {
                    qCritical("Phase 3 FAIL: Manual move in user_edits.usda did not persist across reload!");
                    return 1;
                }
            }
            // Clean up test user_edits.usda so shot_001 stays clean
            viewport.clearAllUserEdits();
            QFile::remove(viewport.userEditsLayerPath());
            qInfo("Phase 2 & 3 PASS: Manual edit written to user_edits.usda, persisted across stage reload, and shot.usda untouched.");
        }

        // Phase 4 Test: Main Scene Edit & Save with Snapshotting and Shot Inheritance
        {
            QTemporaryDir tempSceneDir;
            if (!tempSceneDir.isValid()) {
                qCritical("Phase 4 FAIL: Could not create temporary scene directory");
                return 1;
            }
            const QDir sceneDir(tempSceneDir.path());
            const QString scenePath = sceneDir.filePath("scene.usda");
            const QString shot1DirPath = sceneDir.filePath("shot_001");
            QDir().mkpath(shot1DirPath);
            const QString shot1Path = sceneDir.filePath("shot_001/shot.usda");

            // Baseline scene.usda
            QFile scFile(scenePath);
            if (!scFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
                qCritical("Phase 4 FAIL: Could not write scene.usda");
                return 1;
            }
            scFile.write("#usda 1.0\n(\n    defaultPrim = \"World\"\n)\ndef Xform \"World\"\n{\n    def Xform \"Bench\"\n    {\n        double3 xformOp:translate = (0, 0, 0)\n        uniform token[] xformOpOrder = [\"xformOp:translate\"]\n    }\n}\n");
            scFile.close();

            // Baseline shot_001/shot.usda sublayering ../scene.usda
            QFile shFile(shot1Path);
            if (!shFile.open(QIODevice::WriteOnly | QIODevice::Text)) {
                qCritical("Phase 4 FAIL: Could not write shot.usda");
                return 1;
            }
            shFile.write("#usda 1.0\n(\n    defaultPrim = \"World\"\n    subLayers = [\n        @../scene.usda@\n    ]\n)\nover \"World\"\n{\n}\n");
            shFile.close();

            // Open scene in Scene Mode
            if (!viewport.openStage(scenePath, /*isSceneStage=*/true)) {
                qCritical("Phase 4 FAIL: Could not open scene stage");
                return 1;
            }
            viewport.selectPrim("/World/Bench");
            const QVector3D origBenchPos = viewport.selectedPrimPosition();
            const QVector3D newBenchPos(15.0f, 0.0f, -8.0f);
            viewport.setSelectedPrimPosition(newBenchPos);

            if (viewport.userEditedPrimCount() < 1) {
                qCritical("Phase 4 FAIL: userEditedPrimCount did not increment in scene mode");
                return 1;
            }

            // Save scene edits (should snapshot scene.usda to scene_v1.usda and retarget shot_001)
            QString errorMsg;
            if (!viewport.saveSceneEdits(&errorMsg)) {
                qCritical().noquote() << "Phase 4 FAIL: saveSceneEdits returned false:" << errorMsg;
                return 1;
            }

            // Check that scene_v1.usda exists
            const QString snapV1Path = sceneDir.filePath("scene_v1.usda");
            if (!QFileInfo::exists(snapV1Path)) {
                qCritical("Phase 4 FAIL: scene_v1.usda was not created!");
                return 1;
            }

            // Check that shot_001/shot.usda was retargeted to @../scene_v1.usda@
            QFile shCheck(shot1Path);
            if (!shCheck.open(QIODevice::ReadOnly | QIODevice::Text)) {
                qCritical("Phase 4 FAIL: Could not read shot1 after save");
                return 1;
            }
            const QString shot1Content = QString::fromUtf8(shCheck.readAll());
            shCheck.close();
            if (!shot1Content.contains("@../scene_v1.usda@") && !shot1Content.contains("../scene_v1.usda")) {
                qCritical().noquote() << "Phase 4 FAIL: shot_001 was not retargeted to scene_v1.usda! Content:\n" << shot1Content;
                return 1;
            }

            // Open shot_001 stage and verify Bench is at original frozen position (0, 0, 0)
            viewport.openStage(shot1Path, /*isSceneStage=*/false);
            viewport.selectPrim("/World/Bench");
            const QVector3D benchInShot1 = viewport.selectedPrimPosition();
            if ((benchInShot1 - origBenchPos).length() > 1e-3f) {
                qCritical().noquote() << "Phase 4 FAIL: shot_001 did not preserve frozen scene position! Expected:" << origBenchPos << "got:" << benchInShot1;
                return 1;
            }

            // Create new shot_002 sublayering @../scene.usda@ and verify it inherits newBenchPos
            const QString shot2DirPath = sceneDir.filePath("shot_002");
            QDir().mkpath(shot2DirPath);
            const QString shot2Path = sceneDir.filePath("shot_002/shot.usda");
            QFile sh2File(shot2Path);
            if (sh2File.open(QIODevice::WriteOnly | QIODevice::Text)) {
                sh2File.write("#usda 1.0\n(\n    defaultPrim = \"World\"\n    subLayers = [\n        @../scene.usda@\n    ]\n)\nover \"World\"\n{\n}\n");
                sh2File.close();
            }
            viewport.openStage(shot2Path, /*isSceneStage=*/false);
            viewport.selectPrim("/World/Bench");
            const QVector3D benchInShot2 = viewport.selectedPrimPosition();
            if ((benchInShot2 - newBenchPos).length() > 1e-3f) {
                qCritical().noquote() << "Phase 4 FAIL: new shot_002 did not inherit current scene.usda position! Expected:" << newBenchPos << "got:" << benchInShot2;
                return 1;
            }

            qInfo("Phase 4 PASS: Main scene edit saved, existing shot_001 frozen on scene_v1.usda, new shot_002 inherited scene.usda.");
        }
        return 0;
    }

    // Regression test: simulate real mouse clicks / drags over the USD viewport inside MainWindow.
    const int briefTestArg = arguments.indexOf("--usd-brief-test");
    if (briefTestArg >= 0) {
        QTemporaryDir isolated;
        if (!isolated.isValid()) return 1;
        const QString shot = isolated.filePath("shot.usda");
        { QFile file(shot); if (!file.open(QIODevice::WriteOnly)) return 1;
          file.write(R"USD(#usda 1.0
(
    startTimeCode = 0
    endTimeCode = 24
    timeCodesPerSecond = 24
)
def Xform "World"
{
    def Xform "Hero" (
        customData = { string actor_id = "human_1" }
    )
    {
        def Cube "Body"
        {
            double size = 1
        }
    }
    def Camera "ShotCam" {}
}
)USD"); }
        MainWindow window;
        auto* viewport = window.findChild<UsdViewportWidget*>();
        auto* timeline = window.findChild<ActionTimelineWidget*>();
        if (!viewport || !timeline || !viewport->openStage(shot)) return 1;
        viewport->resize(900,600);
        const auto originalActions = QJsonDocument(timeline->toJson()).toJson();
        ActionSegment action; action.id="test_segment"; action.actorPath="/World/Hero";action.actorLabel="Hero";action.action="walk_to";action.startS=0;action.endS=1;action.pathId="test_path";
        timeline->addAction(action); timeline->selectAction(action.id);
        auto changed=action;changed.endS=2;timeline->updateAction(changed);
        if (!timeline->undo()) { qCritical("Brief test: timeline undo failed"); return 1; }
        bool restored=false;for(const auto& seg:timeline->actions())if(seg.id==action.id && seg.endS==1)restored=true;
        if(!restored){qCritical("Brief test: timeline window restore failed");return 1;}
        if (!timeline->redo()) {qCritical("Brief test: timeline redo failed");return 1;}
        viewport->persistPrimPaths();
        QFile beforeFile(viewport->userEditsLayerPath());if (!beforeFile.open(QIODevice::ReadOnly)) return 1;const auto before=beforeFile.readAll();beforeFile.close();
        viewport->startDrawingPath("/World/Paths/test_path");
        for (int i=0;i<6;++i) {
            const QPointF local(380+i*20,400-i*8);const auto global=viewport->mapToGlobal(local.toPoint());
            QMouseEvent event(i==0?QEvent::MouseButtonPress:QEvent::MouseMove,local,global,i==0?Qt::LeftButton:Qt::NoButton,Qt::LeftButton,Qt::NoModifier);
            QCoreApplication::sendEvent(viewport,&event);
        }
        const QPointF end(480,360);
        QMouseEvent release(QEvent::MouseButtonRelease,end,viewport->mapToGlobal(end.toPoint()),Qt::LeftButton,Qt::NoButton,Qt::NoModifier);QCoreApplication::sendEvent(viewport,&release);
        if (viewport->primPath("/World/Paths/test_path").size()<2) { qCritical("Brief test: synthetic stroke did not produce a path"); return 1; }
        viewport->undo();
        QFile afterFile(viewport->userEditsLayerPath());if (!afterFile.open(QIODevice::ReadOnly)) return 1;
        if (afterFile.readAll()!=before) { qCritical("Brief test: viewport undo did not restore bytes");return 1; }
        timeline->undo();timeline->undo();
        if (QJsonDocument(timeline->toJson()).toJson()!=originalActions) { qCritical("Brief test: timeline bytes did not restore"); return 1; }
        viewport->convertLlmCameraToManualRig();
        const QString cameraFile=isolated.filePath("camera_rig.json");
        QFile baselineCamera(cameraFile);if(!baselineCamera.open(QIODevice::ReadOnly))return 1;
        const auto cameraBefore=baselineCamera.readAll();baselineCamera.close();
        QFile baselineLayer(viewport->userEditsLayerPath());if(!baselineLayer.open(QIODevice::ReadOnly))return 1;
        const auto layerBefore=baselineLayer.readAll();baselineLayer.close();
        viewport->setCameraFocalLength(85);viewport->undo();
        QFile restoredCamera(cameraFile);if(!restoredCamera.open(QIODevice::ReadOnly)||restoredCamera.readAll()!=cameraBefore){qCritical("Brief test: camera sidecar bytes did not restore");return 1;}
        QFile restoredLayer(viewport->userEditsLayerPath());if(!restoredLayer.open(QIODevice::ReadOnly)||restoredLayer.readAll()!=layerBefore){qCritical("Brief test: camera layer bytes did not restore");return 1;}
        viewport->redo();
        if(std::abs(viewport->cameraFocalLength()-85)>.01){qCritical("Brief test: camera redo failed");return 1;}
        const auto rig=createCameraRigPreset("dolly_in"); const auto json=cameraRigToJson(rig);
        const auto loaded=cameraRigFromJson(json);
        if (loaded.points.size()!=rig.points.size() || loaded.focalStart!=rig.focalStart) return 1;
        ActionTimelineWidget movementTimeline;
        ActionSegment walk; walk.id="walk"; walk.actorPath="/World/Hero"; walk.action="walk_to"; walk.startS=0; walk.endS=2;
        ActionSegment follow; follow.id="follow"; follow.actorPath=walk.actorPath; follow.action="wave"; follow.startS=2; follow.endS=3;
        ActionSegment drive; drive.id="drive"; drive.actorPath="/World/Car"; drive.action="drive_to"; drive.startS=0; drive.endS=5;
        movementTimeline.setActions({walk, follow, drive});
        movementTimeline.setMovementDistances({{"walk", 13.0}, {"drive", 20.0}}, false);
        const auto fitted=movementTimeline.actions();
        if (std::abs(fitted[0].endS-9.3)>0.01 || !fitted[0].autoTiming ||
            std::abs(fitted[1].startS-9.3)>0.01 || std::abs(fitted[1].endS-10.3)>0.01 ||
            std::abs(fitted[2].endS-5.0)>0.01 || fitted[2].autoTiming ||
            std::abs(fitted[2].distanceM-20.0)>0.01) {
            qCritical("Brief test: movement timing or distance guide data is wrong"); return 1;
        }
        qInfo("Brief test PASS: synthetic path drawing, lane edits, undo/redo restore, camera JSON bridge, movement timing");
        return 0;
    }

    // Usage: PainterSpace --usd-pick-test [stage.usda]
    const int pickTestArg = arguments.indexOf("--usd-pick-test");
    if (pickTestArg >= 0) {
        MainWindow window;
        window.resize(1400, 900);
        window.show();
        QCoreApplication::processEvents();
        UsdViewportWidget* viewport = window.findChild<UsdViewportWidget*>();
        if (!viewport) {
            qCritical("Pick test FAIL: UsdViewportWidget not found");
            return 1;
        }
        QString stagePath = arguments.size() > pickTestArg + 1 ? arguments.at(pickTestArg + 1) : QString();
        if (stagePath.isEmpty() || stagePath.startsWith("--")) {
            stagePath = locateRepoFile(QDir::currentPath(), "scenes/scene_01/shot_001/shot.usda");
        }
        if (!viewport->hasStage() && (stagePath.isEmpty() || !viewport->openStage(stagePath))) {
            qCritical().noquote() << "Pick test FAIL: could not open stage" << stagePath;
            return 1;
        }
        for (QWidget* w = viewport; w && w->parentWidget(); w = w->parentWidget()) {
            if (auto* stack = qobject_cast<QStackedWidget*>(w->parentWidget())) {
                stack->setCurrentWidget(w);
            }
        }
        viewport->show();
        viewport->repaint();
        QCoreApplication::processEvents();
        qInfo().noquote() << "Pick test: viewport" << viewport->size() << "visible=" << viewport->isVisible();

        // Snapshot user_edits.usda so the simulated drags can be reverted byte-for-byte afterwards.
        const QString userEditsPath = viewport->userEditsLayerPath();
        QByteArray userEditsBefore;
        {
            QFile f(userEditsPath);
            if (f.open(QIODevice::ReadOnly)) userEditsBefore = f.readAll();
        }

        int selections = 0;
        const int cols = 9, rows = 6;
        for (int r = 1; r < rows; ++r) {
            for (int c = 1; c < cols; ++c) {
                const QPointF local(viewport->width() * c / double(cols), viewport->height() * r / double(rows));
                const QPointF global = viewport->mapToGlobal(local);
                QMouseEvent press(QEvent::MouseButtonPress, local, global, Qt::LeftButton, Qt::LeftButton, Qt::NoModifier);
                QCoreApplication::sendEvent(viewport, &press);
                viewport->repaint();
                QCoreApplication::processEvents();
                const QPointF moved = local + QPointF(12.0, 6.0);
                QMouseEvent move(QEvent::MouseMove, moved, viewport->mapToGlobal(moved), Qt::NoButton, Qt::LeftButton, Qt::NoModifier);
                QCoreApplication::sendEvent(viewport, &move);
                viewport->repaint();
                QMouseEvent release(QEvent::MouseButtonRelease, moved, viewport->mapToGlobal(moved), Qt::LeftButton, Qt::NoButton, Qt::NoModifier);
                QCoreApplication::sendEvent(viewport, &release);
                viewport->repaint();
                QCoreApplication::processEvents();
                if (!viewport->selectedPrimPath().isEmpty()) {
                    ++selections;
                    qInfo().noquote() << "Pick test: clicked" << local << "->" << viewport->selectedPrimPath();
                }
            }
        }
        // Revert the moves performed by the simulated drags so the shot is left untouched.
        if (!userEditsPath.isEmpty()) {
            QSaveFile f(userEditsPath);
            if (f.open(QIODevice::WriteOnly)) {
                f.write(userEditsBefore);
                f.commit();
            }
        }
        qInfo().noquote() << "Pick test PASS: no crash," << selections << "clicks selected a prim.";
        return 0;
    }

    MainWindow window;
    window.show();
    return app.exec();
}
