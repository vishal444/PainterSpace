#include "MainWindow.h"
#include "AssetTypes.h"
#include "CameraRig.h"
#include "CameraTools.h"
#include "ShotEngine.h"
#include "UsdViewportWidget.h"
#include <QApplication>
#include <cmath>
#include <QDoubleSpinBox>
#include <QCoreApplication>
#include <QDialog>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QComboBox>
#include <QCheckBox>
#include <QFrame>
#include <QFontComboBox>
#include <QFileInfo>
#include <QFileSystemWatcher>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QIcon>
#include <QImage>
#include <QLabel>
#include <QLineEdit>
#include <QInputDialog>
#include <QTableWidget>
#include <QTabWidget>
#include <QDialogButtonBox>
#include <QMenu>
#include <QListWidget>
#include <QListWidgetItem>
#include <QMessageBox>
#include <QPixmap>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSaveFile>
#include <QScrollArea>
#include <QSlider>
#include <QSignalBlocker>
#include <QStackedWidget>
#include <QStandardPaths>
#include <QStringList>
#include <QTimer>
#include <QPainter>
#include <QPainterPath>
#include <QToolButton>
#include <QUuid>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <algorithm>

namespace {
// The storyboard starts as a 3×3 book and grows when further shots are added.
constexpr int kInitialStoryboardSlots = 9;

class TypedTransformSpinBox : public QDoubleSpinBox {
public:
    explicit TypedTransformSpinBox(QWidget* parent) : QDoubleSpinBox(parent) {
        setKeyboardTracking(false);
        setButtonSymbols(QAbstractSpinBox::NoButtons);
    }

protected:
    void wheelEvent(QWheelEvent* event) override { event->ignore(); }
    void stepBy(int) override {} // Arrow keys must not change transforms either.
};

static QIcon createDrawPathPencilIcon(bool active) {
    QPixmap pixmap(64, 64);
    pixmap.fill(Qt::transparent);
    QPainter p(&pixmap);
    p.setRenderHint(QPainter::Antialiasing, true);

    const QColor leadColor = active ? QColor("#79c0ff") : QColor("#e6edf3");
    const QColor bodyColor = active ? QColor("#1f6feb") : QColor("#d97706");
    const QColor woodColor = QColor("#fde68a");
    const QColor eraserColor = QColor("#f43f5e");
    const QColor pathColor = active ? QColor("#38bdf8") : QColor("#8b949e");

    // 1. Draw smooth curved camera motion path beneath the pencil
    QPainterPath motionPath;
    motionPath.moveTo(8, 54);
    motionPath.cubicTo(14, 38, 22, 58, 30, 44);
    p.setPen(QPen(pathColor, 3.0, Qt::DashLine, Qt::RoundCap, Qt::RoundJoin));
    p.drawPath(motionPath);

    // Path waypoints
    p.setPen(Qt::NoPen);
    p.setBrush(pathColor);
    p.drawEllipse(QPointF(8, 54), 3.0, 3.0);
    p.drawEllipse(QPointF(19, 48), 2.5, 2.5);

    // 2. Draw pencil angled from top-right towards bottom-left tip at (30, 44)
    p.save();
    p.translate(30, 44);
    p.rotate(-45);

    // Wood cone
    QPolygonF woodCone;
    woodCone << QPointF(0, 0) << QPointF(-5.5, -9) << QPointF(5.5, -9);
    p.setBrush(woodColor);
    p.setPen(QPen(QColor("#b45309"), 1.0));
    p.drawPolygon(woodCone);

    // Graphite lead tip
    QPolygonF leadTip;
    leadTip << QPointF(0, 0) << QPointF(-2.5, -4) << QPointF(2.5, -4);
    p.setBrush(leadColor);
    p.setPen(Qt::NoPen);
    p.drawPolygon(leadTip);

    // Pencil shaft
    QRectF shaft(-5.5, -34, 11, 25);
    p.setBrush(bodyColor);
    p.setPen(QPen(bodyColor.darker(130), 1.0));
    p.drawRect(shaft);

    // Stripe highlight
    p.setPen(QPen(bodyColor.lighter(135), 2.0));
    p.drawLine(QPointF(0, -9), QPointF(0, -34));

    // Metal ferrule
    QRectF ferrule(-5.5, -39, 11, 5);
    p.setBrush(QColor("#cbd5e1"));
    p.setPen(QPen(QColor("#94a3b8"), 1.0));
    p.drawRect(ferrule);

    // Eraser
    p.setBrush(eraserColor);
    p.setPen(QPen(eraserColor.darker(120), 1.0));
    p.drawRoundedRect(QRectF(-5.5, -45, 11, 6), 2.0, 2.0);

    p.restore();
    return QIcon(pixmap);
}

QString panelStyle() {
    return "QGroupBox { color: #a9b3bf; font-size: 11px; font-weight: 700; "
           "background: #15191f; border: 1px solid #303945; border-radius: 8px; margin-top: 12px; padding: 16px 10px 10px; }"
           "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }";
}

QString directorPanelStyle() {
    return "QGroupBox { color: #c9d1d9; font-size: 11px; font-weight: 700; "
           "background: #15191f; border: 1px solid #28303a; border-radius: 8px; margin-top: 6px; padding: 10px 8px 8px; }"
           "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #58a6ff; }"
           "QComboBox { background: #1c2128; color: #c9d1d9; border: 1px solid #343e4b; border-radius: 5px; padding: 2px 6px; min-height: 24px; font-size: 11px; }"
           "QComboBox:hover { border-color: #58a6ff; }"
           "QComboBox:disabled { background: #13171c; color: #484f58; border-color: #21262d; }"
           "QComboBox::drop-down { border: none; width: 16px; }"
           "QComboBox QAbstractItemView { background: #1c2128; color: #c9d1d9; selection-background-color: #2b3644; selection-color: #58a6ff; border: 1px solid #343e4b; outline: none; }"
           "QDoubleSpinBox { background: #1c2128; color: #c9d1d9; border: 1px solid #343e4b; border-radius: 5px; padding: 2px 4px; min-height: 22px; font-size: 11px; }"
           "QDoubleSpinBox:hover { border-color: #58a6ff; }"
           "QDoubleSpinBox:focus { border-color: #58a6ff; }"
           "QDoubleSpinBox:disabled { background: #13171c; color: #484f58; border: 1px solid #21262d; }"
           "QDoubleSpinBox QLineEdit { background: transparent; color: #c9d1d9; border: none; font-size: 11px; selection-background-color: #2b3644; selection-color: #58a6ff; }"
           "QDoubleSpinBox QLineEdit:disabled { background: transparent; color: #484f58; border: none; }"
           "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 0px; border: none; }"
           "QDoubleSpinBox#spinX { border-left: 2px solid #f85149; }"
           "QDoubleSpinBox#spinY { border-left: 2px solid #3fb950; }"
           "QDoubleSpinBox#spinZ { border-left: 2px solid #58a6ff; }"
           "QDoubleSpinBox#spinScale { border-left: 2px solid #d29922; }"
           "QDoubleSpinBox#spinX:disabled, QDoubleSpinBox#spinY:disabled, QDoubleSpinBox#spinZ:disabled, QDoubleSpinBox#spinScale:disabled { border-left: 2px solid #2b333c; }"
           "QCheckBox { color: #8b949e; font-size: 11px; min-height: 20px; spacing: 5px; }"
           "QCheckBox:hover { color: #c9d1d9; }"
           "QCheckBox::indicator { width: 13px; height: 13px; border-radius: 3px; border: 1px solid #343e4b; background: #1c2128; }"
           "QCheckBox::indicator:checked { background: #2563eb; border-color: #60a5fa; }"
           "QLabel { color: #8b949e; font-size: 11px; }";
}

QString toolStyle() {
    return "QPushButton { color: #b5bec9; background: #20262e; border: 1px solid #343e4b; "
           "border-radius: 6px; padding: 9px 8px; text-align: left; }"
           "QPushButton:hover { background: #29323d; }"
           "QPushButton:checked { color: #f7f9fb; background: #39536b; border-color: #73b5da; }";
}

QByteArray defaultSceneUsdaSource() {
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
           "        float inputs:intensity = 0.45\n"
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
           "            double size = 1\n"
           "            color3f[] primvars:displayColor = [(0.16, 0.20, 0.24)] (\n"
           "                interpolation = \"constant\"\n"
           "            )\n"
           "        }\n"
           "    }\n"
           "}\n";
}

QByteArray defaultShotUsdaSource() {
    return "#usda 1.0\n"
           "(\n"
           "    defaultPrim = \"World\"\n"
           "    upAxis = \"Y\"\n"
           "    metersPerUnit = 1\n"
           "    timeCodesPerSecond = 24\n"
           "    startTimeCode = 0\n"
           "    endTimeCode = 48\n"
           "    subLayers = [\n"
           "        @../scene.usda@\n"
           "    ]\n"
           ")\n\n"
           "over \"World\"\n"
           "{\n"
           "    def Camera \"ShotCam\"\n"
           "    {\n"
           "        double3 xformOp:translate = (0, 2.2, 10.0)\n"
           "        float3 xformOp:rotateXYZ = (-8.0, 0.0, 0.0)\n"
           "        uniform token[] xformOpOrder = [\"xformOp:translate\", \"xformOp:rotateXYZ\"]\n"
           "        float focalLength = 35\n"
           "        float horizontalAperture = 36\n"
           "        float verticalAperture = 20.25\n"
           "        float2 clippingRange = (0.1, 1000)\n"
           "    }\n"
           "}\n";
}

QString painterSpaceProjectRoot() {
    QDir directory(QCoreApplication::applicationDirPath());
    do {
        if (QFileInfo::exists(directory.filePath("manifest.json")) ||
            QFileInfo::exists(directory.filePath("assets/manifest.json"))) {
            return directory.absolutePath();
        }
    } while (directory.cdUp());
    return QDir::currentPath();
}

QString worldStatePythonFor(const QString& projectRoot) {
    const QString configured = qEnvironmentVariable("PAINTERSPACE_PYTHON");
    if (!configured.isEmpty()) return configured;
    const QString venv = QDir(projectRoot).filePath(".venv/bin/python3");
    if (QFileInfo::exists(venv)) return venv;
    return QStandardPaths::findExecutable("python3");
}

QJsonObject readJsonFile(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    return QJsonDocument::fromJson(file.readAll()).object();
}

void syncManifestForProject(const QString& projectRoot) {
    const QString script = QDir(projectRoot).filePath("tools/sync_manifest.py");
    const QString python = worldStatePythonFor(projectRoot);
    if (!QFileInfo::exists(script) || python.isEmpty()) return;
    QProcess proc;
    proc.setWorkingDirectory(projectRoot);
    proc.start(python, {script, QStringLiteral("--project-root"), projectRoot, QStringLiteral("--quiet")});
    proc.waitForFinished(15000);
}
}

MainWindow::MainWindow(QWidget* parent) : QMainWindow(parent) {
    setWindowTitle("PainterSpace — Concept Studio");
    resize(1540, 900);
    setMinimumSize(1180, 700);

    myViewStack = new QStackedWidget(this);
    myViewStack->setObjectName("workspace");
    setCentralWidget(myViewStack);

    myProjectPage = new QWidget(myViewStack);
    myProjectPage->setObjectName("projectPage");
    QVBoxLayout* projectLayout = new QVBoxLayout(myProjectPage);
    projectLayout->setContentsMargins(80, 54, 80, 54);
    projectLayout->setSpacing(18);
    QLabel* projectMark = new QLabel("P", myProjectPage);
    projectMark->setObjectName("brandMark");
    projectMark->setAlignment(Qt::AlignCenter);
    projectMark->setFixedSize(48, 48);
    projectLayout->addWidget(projectMark, 0, Qt::AlignLeft);
    QLabel* explorerTitle = new QLabel("Project Explorer", myProjectPage);
    explorerTitle->setObjectName("appTitle");
    projectLayout->addWidget(explorerTitle);
    QLabel* explorerHint = new QLabel("Create a new project or open an existing project to view your list of scenes as a grid.", myProjectPage);
    explorerHint->setObjectName("mutedText");
    projectLayout->addWidget(explorerHint);

    QFrame* createProjectCard = new QFrame(myProjectPage);
    createProjectCard->setObjectName("projectCard");
    createProjectCard->setMaximumWidth(760);
    QHBoxLayout* createProjectLayout = new QHBoxLayout(createProjectCard);
    createProjectLayout->setContentsMargins(22, 18, 22, 18);
    createProjectLayout->setSpacing(14);
    QLabel* createProjectIcon = new QLabel("＋", createProjectCard);
    createProjectIcon->setObjectName("projectIcon");
    createProjectIcon->setAlignment(Qt::AlignCenter);
    createProjectIcon->setFixedSize(52, 52);
    createProjectLayout->addWidget(createProjectIcon);
    QVBoxLayout* createProjectInfo = new QVBoxLayout();
    QLabel* createProjectTitle = new QLabel("Create New Project", createProjectCard);
    createProjectTitle->setObjectName("cardTitle");
    QLabel* createProjectSub = new QLabel("Starts a new project with a grid of scenes  •  Describe each scene first, then build individual shots", createProjectCard);
    createProjectSub->setObjectName("mutedText");
    myNewProjectNameEdit = new QLineEdit(createProjectCard);
    myNewProjectNameEdit->setPlaceholderText("Project title (e.g., My Storyboard Project)…");
    createProjectInfo->addWidget(createProjectTitle);
    createProjectInfo->addWidget(createProjectSub);
    createProjectInfo->addWidget(myNewProjectNameEdit);
    createProjectLayout->addLayout(createProjectInfo, 1);
    QPushButton* createProjectButton = new QPushButton("＋  Create project  →", createProjectCard);
    createProjectButton->setObjectName("primaryButton");
    createProjectButton->setCursor(Qt::PointingHandCursor);
    createProjectLayout->addWidget(createProjectButton);
    connect(createProjectButton, &QPushButton::clicked, this, &MainWindow::createStoryboardProject);
    projectLayout->addWidget(createProjectCard);

    QFrame* projectCard = new QFrame(myProjectPage);
    projectCard->setObjectName("projectCard");
    projectCard->setMaximumWidth(760);
    QHBoxLayout* projectCardLayout = new QHBoxLayout(projectCard);
    projectCardLayout->setContentsMargins(22, 18, 22, 18);
    QLabel* projectIcon = new QLabel("▦", projectCard);
    projectIcon->setObjectName("projectIcon");
    projectIcon->setAlignment(Qt::AlignCenter);
    projectIcon->setFixedSize(52, 52);
    projectCardLayout->addWidget(projectIcon);
    QVBoxLayout* projectInfo = new QVBoxLayout();
    QLabel* projectName = new QLabel("Storyboard Project", projectCard);
    projectName->setObjectName("cardTitle");
    QLabel* projectDescription = new QLabel("Existing project  •  Scenes grid & 3D / LLM shots", projectCard);
    projectDescription->setObjectName("mutedText");
    projectInfo->addWidget(projectName);
    projectInfo->addWidget(projectDescription);
    projectCardLayout->addLayout(projectInfo, 1);
    QPushButton* openProjectButton = new QPushButton("Open project  →", projectCard);
    openProjectButton->setObjectName("quietButton");
    openProjectButton->setCursor(Qt::PointingHandCursor);
    projectCardLayout->addWidget(openProjectButton);
    connect(openProjectButton, &QPushButton::clicked, this, &MainWindow::openStoryboardProject);
    projectLayout->addWidget(projectCard);

    projectLayout->addStretch(1);
    myViewStack->addWidget(myProjectPage);

    myScenesPage = new QWidget(myViewStack);
    myScenesPage->setObjectName("scenesPage");
    QVBoxLayout* scenesLayout = new QVBoxLayout(myScenesPage);
    scenesLayout->setContentsMargins(34, 26, 34, 30);
    scenesLayout->setSpacing(16);
    QHBoxLayout* scenesHeader = new QHBoxLayout();
    QPushButton* scenesBackToProjects = new QPushButton("← Projects", myScenesPage);
    scenesBackToProjects->setObjectName("quietButton");
    scenesBackToProjects->setCursor(Qt::PointingHandCursor);
    connect(scenesBackToProjects, &QPushButton::clicked, this, &MainWindow::showProjectExplorer);
    scenesHeader->addWidget(scenesBackToProjects);
    myScenesTitle = new QLabel("Storyboard Scenes", myScenesPage);
    myScenesTitle->setObjectName("appTitle");
    scenesHeader->addWidget(myScenesTitle);
    scenesHeader->addStretch();
    QPushButton* newSceneButton = new QPushButton("＋  New scene", myScenesPage);
    newSceneButton->setObjectName("primaryButton");
    newSceneButton->setCursor(Qt::PointingHandCursor);
    connect(newSceneButton, &QPushButton::clicked, this, &MainWindow::addStoryboardScene);
    scenesHeader->addWidget(newSceneButton);
    scenesLayout->addLayout(scenesHeader);
    QLabel* scenesHint = new QLabel("Click a scene to first describe it (via LLM prompt or 3D editor) before moving into its individual shots.", myScenesPage);
    scenesHint->setObjectName("mutedText");
    scenesLayout->addWidget(scenesHint);
    QScrollArea* scenesScroll = new QScrollArea(myScenesPage);
    scenesScroll->setObjectName("bookSheet");
    scenesScroll->setWidgetResizable(true);
    scenesScroll->setFrameStyle(QFrame::NoFrame);
    scenesScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    scenesScroll->setStyleSheet("QScrollArea#bookSheet { background: #1b2027; border: 1px solid #303945; border-radius: 12px; }");
    QWidget* scenesGridWidget = new QWidget(scenesScroll);
    myScenesGrid = new QGridLayout(scenesGridWidget);
    myScenesGrid->setContentsMargins(4, 4, 4, 4);
    myScenesGrid->setHorizontalSpacing(18);
    myScenesGrid->setVerticalSpacing(18);
    scenesScroll->setWidget(scenesGridWidget);
    scenesLayout->addWidget(scenesScroll, 1);
    myViewStack->addWidget(myScenesPage);

    myBookPage = new QWidget(myViewStack);
    myBookPage->setObjectName("bookPage");
    QVBoxLayout* bookLayout = new QVBoxLayout(myBookPage);
    bookLayout->setContentsMargins(34, 26, 34, 30);
    bookLayout->setSpacing(16);
    QHBoxLayout* bookHeader = new QHBoxLayout();
    QPushButton* backToScenes = new QPushButton("← Scenes", myBookPage);
    backToScenes->setObjectName("quietButton");
    backToScenes->setCursor(Qt::PointingHandCursor);
    connect(backToScenes, &QPushButton::clicked, this, &MainWindow::showStoryboardScenes);
    bookHeader->addWidget(backToScenes);
    QPushButton* editSceneButton = new QPushButton("✎  Edit Main Scene", myBookPage);
    editSceneButton->setObjectName("quietButton");
    editSceneButton->setCursor(Qt::PointingHandCursor);
    editSceneButton->setToolTip("Edit the shared main scene (scene.usda). Saved edits reflect on new shots while keeping existing shots frozen.");
    connect(editSceneButton, &QPushButton::clicked, this, [this]() {
        if (myCurrentScene >= 0) openSceneSetup(myCurrentScene);
    });
    bookHeader->addWidget(editSceneButton);
    myBookTitle = new QLabel("Individual Shots", myBookPage);
    myBookTitle->setObjectName("appTitle");
    bookHeader->addWidget(myBookTitle);
    bookHeader->addStretch();
    myBookLastEdited = new QLabel("No shots yet", myBookPage);
    myBookLastEdited->setObjectName("mutedText");
    bookHeader->addWidget(myBookLastEdited);
    QPushButton* newPanelButton = new QPushButton("＋  New shot", myBookPage);
    newPanelButton->setObjectName("primaryButton");
    newPanelButton->setCursor(Qt::PointingHandCursor);
    connect(newPanelButton, &QPushButton::clicked, this, &MainWindow::startStoryboardPanel);
    bookHeader->addWidget(newPanelButton);
    bookLayout->addLayout(bookHeader);
    myBookSceneSummary = new QLabel("All shots in this grid inherit the shared scene description and 3D layout.", myBookPage);
    myBookSceneSummary->setObjectName("mutedText");
    myBookSceneSummary->setWordWrap(true);
    bookLayout->addWidget(myBookSceneSummary);
    QScrollArea* bookScroll = new QScrollArea(myBookPage);
    bookScroll->setObjectName("bookSheet");
    bookScroll->setWidgetResizable(true);
    bookScroll->setFrameStyle(QFrame::NoFrame);
    bookScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    bookScroll->setStyleSheet("QScrollArea#bookSheet { background: #1b2027; border: 1px solid #303945; border-radius: 12px; }");
    QWidget* bookGridWidget = new QWidget(bookScroll);
    myBookGrid = new QGridLayout(bookGridWidget);
    myBookGrid->setContentsMargins(4, 4, 4, 4);
    myBookGrid->setHorizontalSpacing(18);
    myBookGrid->setVerticalSpacing(18);
    bookScroll->setWidget(bookGridWidget);
    bookLayout->addWidget(bookScroll, 1);
    myViewStack->addWidget(myBookPage);

    myEditorPage = new QWidget(myViewStack);
    myEditorPage->setObjectName("editorPage");
    myViewStack->addWidget(myEditorPage);
    QVBoxLayout* root = new QVBoxLayout(myEditorPage);
    root->setContentsMargins(18, 14, 18, 16);
    root->setSpacing(10);

    QHBoxLayout* editorHeader = new QHBoxLayout();
    editorHeader->setSpacing(10);
    myEditorBackButton = new QPushButton("← Shots", myEditorPage);
    myEditorBackButton->setObjectName("quietButton");
    myEditorBackButton->setCursor(Qt::PointingHandCursor);
    connect(myEditorBackButton, &QPushButton::clicked, this, [this]() {
        if (myCurrentPanel < 0) {
            if (myCurrentScene >= 0 && myCurrentScene < myStoryboardScenes.size()) {
                if (myScenePrompt) myStoryboardScenes[myCurrentScene].prompt = myScenePrompt->toPlainText().trimmed();
            }
            if (myCurrentScene >= 0 && myCurrentScene < myStoryboardScenes.size() && myStoryboardScenes.at(myCurrentScene).configured) {
                showStoryboardBook();
            } else {
                showStoryboardScenes();
            }
        } else {
            showStoryboardBook();
        }
    });
    editorHeader->addWidget(myEditorBackButton);

    myEditorEditSceneButton = new QPushButton("✎  Edit Main Scene", myEditorPage);
    myEditorEditSceneButton->setObjectName("quietButton");
    myEditorEditSceneButton->setCursor(Qt::PointingHandCursor);
    myEditorEditSceneButton->setToolTip("Open and edit the shared main scene (scene.usda)");
    connect(myEditorEditSceneButton, &QPushButton::clicked, this, [this]() {
        if (myCurrentScene >= 0) {
            if (myCurrentPanel >= 0 && myCurrentPanel < storyboardPanels().size()) {
                if (myScenePrompt) storyboardPanels()[myCurrentPanel].prompt = myScenePrompt->toPlainText().trimmed();
            }
            openSceneSetup(myCurrentScene);
        }
    });
    editorHeader->addWidget(myEditorEditSceneButton);

    myEditorSaveSceneButton = new QPushButton("Save Scene", myEditorPage);
    myEditorSaveSceneButton->setObjectName("quietButton");
    myEditorSaveSceneButton->setCursor(Qt::PointingHandCursor);
    myEditorSaveSceneButton->setToolTip("Save main scene edits to scene.usda");
    connect(myEditorSaveSceneButton, &QPushButton::clicked, this, &MainWindow::saveCurrentSceneEdits);
    editorHeader->addWidget(myEditorSaveSceneButton);

    myEditorUndoButton = new QPushButton("↶  Undo", myEditorPage);
    myEditorUndoButton->setObjectName("quietButton");
    myEditorUndoButton->setCursor(Qt::PointingHandCursor);
    myEditorUndoButton->setToolTip("Undo last action (Cmd+Z / Ctrl+Z)");
    myEditorUndoButton->setShortcut(QKeySequence::Undo);
    connect(myEditorUndoButton, &QPushButton::clicked, this, &MainWindow::performGlobalUndo);
    editorHeader->addWidget(myEditorUndoButton);

    myEditorPrimaryActionButton = new QPushButton("Save Shot", myEditorPage);
    myEditorPrimaryActionButton->setObjectName("renderButton");
    myEditorPrimaryActionButton->setCursor(Qt::PointingHandCursor);
    connect(myEditorPrimaryActionButton, &QPushButton::clicked, this, [this]() {
        if (myCurrentPanel < 0) {
            confirmSceneSetupAndOpenShots();
        } else {
            renderScene();
        }
    });
    editorHeader->addWidget(myEditorPrimaryActionButton);
    root->addLayout(editorHeader);

    // Hidden status label to retain caller compatibility
    myCanvasStatus = new QLabel(myEditorPage);
    myCanvasStatus->setVisible(false);

    QHBoxLayout* promptRow = new QHBoxLayout();
    promptRow->setSpacing(10);
    myScenePrompt = new QPlainTextEdit(myEditorPage);
    myScenePrompt->setObjectName("storyboardScenePrompt");
    myScenePrompt->setFixedHeight(54);
    myScenePrompt->setPlaceholderText("Describe this shot…");
    myScenePrompt->setStyleSheet("QPlainTextEdit { background: #10151a; color: #e7edf3; border: 1px solid #303945; border-radius: 7px; padding: 8px 10px; font-size: 13px; }");
    promptRow->addWidget(myScenePrompt, 1);
    myGenerateUsdButton = new QPushButton("✦  Generate Shot", myEditorPage);
    myGenerateUsdButton->setObjectName("primaryButton");
    myGenerateUsdButton->setMinimumWidth(180);
    myGenerateUsdButton->setFixedHeight(54);
    myGenerateUsdButton->setCursor(Qt::PointingHandCursor);
    promptRow->addWidget(myGenerateUsdButton);
    connect(myGenerateUsdButton, &QPushButton::clicked, this, &MainWindow::generateUsdForCurrentPanel);
    root->addLayout(promptRow);

    QHBoxLayout* columns = new QHBoxLayout();
    columns->setSpacing(14);

    QWidget* leftSidebar = new QWidget(this);
    leftSidebar->setFixedWidth(280);
    QVBoxLayout* leftLayout = new QVBoxLayout(leftSidebar);
    leftLayout->setContentsMargins(0, 0, 0, 0);
    leftLayout->setSpacing(8);

    // Dedicated Camera Director panel on left sidebar
    QScrollArea* cameraScroll = new QScrollArea(leftSidebar);
    cameraScroll->setWidgetResizable(true);
    cameraScroll->setFrameStyle(QFrame::NoFrame);
    cameraScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    cameraScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    cameraScroll->setStyleSheet(
        "QScrollArea { background: #15191e; border: none; } "
        "QScrollArea > QWidget { background: #15191e; } "
        "QScrollBar:vertical { background: #15191e; width: 6px; margin: 0; } "
        "QScrollBar::handle:vertical { background: #28303a; min-height: 20px; border-radius: 3px; } "
        "QScrollBar::handle:vertical:hover { background: #388bfd; } "
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; } "
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }");
    cameraScroll->viewport()->setStyleSheet("background: #15191e; border: none;");

    QWidget* cameraContainer = new QWidget(cameraScroll);
    cameraContainer->setStyleSheet("background: #15191e; border: none;");
    QVBoxLayout* cameraLayout = new QVBoxLayout(cameraContainer);
    cameraLayout->setContentsMargins(0, 0, 0, 0);
    cameraLayout->setSpacing(4);

    setupCameraInspector(cameraLayout);
    cameraLayout->addStretch(1);

    cameraScroll->setWidget(cameraContainer);
    leftLayout->addWidget(cameraScroll, 1);

    columns->addWidget(leftSidebar);

    myUsdViewport = new UsdViewportWidget(this);
    myUsdViewport->setObjectName("usdViewport");
    columns->addWidget(myUsdViewport, 1);

    QWidget* rightSidebar = new QWidget(this);
    rightSidebar->setFixedWidth(280);
    QVBoxLayout* rightLayout = new QVBoxLayout(rightSidebar);
    rightLayout->setContentsMargins(0, 0, 0, 0);
    rightLayout->setSpacing(4);

    // Scrollable container for Layers / Prim Tree AND Prim Transform Inspector
    QScrollArea* upperScroll = new QScrollArea(rightSidebar);
    upperScroll->setWidgetResizable(true);
    upperScroll->setFrameStyle(QFrame::NoFrame);
    upperScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    upperScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    upperScroll->setStyleSheet(
        "QScrollArea { background: #15191e; border: none; } "
        "QScrollArea > QWidget { background: #15191e; } "
        "QScrollBar:vertical { background: #15191e; width: 6px; margin: 0; } "
        "QScrollBar::handle:vertical { background: #28303a; min-height: 20px; border-radius: 3px; } "
        "QScrollBar::handle:vertical:hover { background: #388bfd; } "
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; } "
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }");
    upperScroll->viewport()->setStyleSheet("background: #15191e; border: none;");

    QWidget* upperContainer = new QWidget(upperScroll);
    upperContainer->setStyleSheet("background: #15191e; border: none;");
    upperContainer->setMinimumWidth(0);
    QVBoxLayout* upperLayout = new QVBoxLayout(upperContainer);
    upperLayout->setContentsMargins(0, 0, 0, 0);
    upperLayout->setSpacing(6);

    QGroupBox* layersBox = new QGroupBox("LAYERS / PRIM TREE", upperContainer);
    layersBox->setStyleSheet(directorPanelStyle());
    layersBox->setMinimumHeight(110);
    layersBox->setMaximumHeight(180);
    layersBox->setMinimumWidth(0);
    QVBoxLayout* layersLayout = new QVBoxLayout(layersBox);
    layersLayout->setContentsMargins(6, 10, 6, 6);
    myLayers = new QListWidget(layersBox);
    myLayers->setObjectName("layersList");
    myLayers->setMinimumWidth(0);
    myLayers->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    myLayers->setTextElideMode(Qt::ElideMiddle);
    myLayers->setStyleSheet(
        "QListWidget#layersList { background: #1c2128; border: 1px solid #28303a; border-radius: 6px; padding: 3px; color: #c9d1d9; } "
        "QListWidget#layersList::item { padding: 5px 6px; color: #bac4ce; border-radius: 4px; } "
        "QListWidget#layersList::item:hover { background: #222933; color: #ffffff; } "
        "QListWidget#layersList::item:selected { background: #263342; color: #58a6ff; font-weight: 600; }");
    layersLayout->addWidget(myLayers);
    upperLayout->addWidget(layersBox);

    // Prim Inspector
    QGroupBox* inspectorBox = new QGroupBox("PRIM TRANSFORM", upperContainer);
    inspectorBox->setStyleSheet(directorPanelStyle());
    inspectorBox->setMinimumWidth(0);
    QVBoxLayout* inspectorLayout = new QVBoxLayout(inspectorBox);
    inspectorLayout->setContentsMargins(8, 10, 8, 8);
    inspectorLayout->setSpacing(6);

    mySelectionName = new QLabel("No asset selected", inspectorBox);
    mySelectionName->setObjectName("selectionName");
    mySelectionName->setStyleSheet("background: #1c2128; color: #58a6ff; font-weight: 600; "
                                   "border: 1px solid #30363d; border-radius: 5px; padding: 4px 8px; font-size: 11px;");
    mySelectionName->setWordWrap(true);
    mySelectionName->setMinimumWidth(0);
    mySelectionName->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    inspectorLayout->addWidget(mySelectionName);

    // Position Row: Pos + X + Y + Z
    QHBoxLayout* posRow = new QHBoxLayout();
    posRow->setSpacing(4);
    QLabel* posLbl = new QLabel("Pos", inspectorBox);
    posLbl->setFixedWidth(26);
    posLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    posRow->addWidget(posLbl);

    myPositionX = new TypedTransformSpinBox(inspectorBox);
    myPositionY = new TypedTransformSpinBox(inspectorBox);
    myPositionZ = new TypedTransformSpinBox(inspectorBox);
    myPositionX->setObjectName("spinX");
    myPositionY->setObjectName("spinY");
    myPositionZ->setObjectName("spinZ");
    myPositionX->setPrefix("X ");
    myPositionY->setPrefix("Y ");
    myPositionZ->setPrefix("Z ");
    for (QDoubleSpinBox* spin : {myPositionX, myPositionY, myPositionZ}) {
        spin->setRange(-500.0, 500.0);
        spin->setSingleStep(0.1);
        spin->setDecimals(2);
        spin->setSuffix("m");
        spin->setButtonSymbols(QAbstractSpinBox::NoButtons);
        spin->setAlignment(Qt::AlignCenter);
        spin->setMinimumWidth(40);
        spin->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        posRow->addWidget(spin, 1);
    }
    inspectorLayout->addLayout(posRow);

    // Rotation Row: Rot + X + Y + Z
    QHBoxLayout* rotRow = new QHBoxLayout();
    rotRow->setSpacing(4);
    QLabel* rotLbl = new QLabel("Rot", inspectorBox);
    rotLbl->setFixedWidth(26);
    rotLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    rotRow->addWidget(rotLbl);

    myRotationX = new TypedTransformSpinBox(inspectorBox);
    myRotationY = new TypedTransformSpinBox(inspectorBox);
    myRotationZ = new TypedTransformSpinBox(inspectorBox);
    myRotationX->setObjectName("spinX");
    myRotationY->setObjectName("spinY");
    myRotationZ->setObjectName("spinZ");
    myRotationX->setPrefix("X ");
    myRotationY->setPrefix("Y ");
    myRotationZ->setPrefix("Z ");
    for (QDoubleSpinBox* spin : {myRotationX, myRotationY, myRotationZ}) {
        spin->setRange(-360.0, 360.0);
        spin->setSingleStep(5.0);
        spin->setDecimals(1);
        spin->setSuffix("°");
        spin->setButtonSymbols(QAbstractSpinBox::NoButtons);
        spin->setAlignment(Qt::AlignCenter);
        spin->setMinimumWidth(40);
        spin->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
        rotRow->addWidget(spin, 1);
    }
    inspectorLayout->addLayout(rotRow);

    // Scale Row: Scale + spin
    QHBoxLayout* scaleRow = new QHBoxLayout();
    scaleRow->setSpacing(4);
    QLabel* scaleLbl = new QLabel("Scale", inspectorBox);
    scaleLbl->setFixedWidth(26);
    scaleLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    scaleRow->addWidget(scaleLbl);

    myScaleSpin = new TypedTransformSpinBox(inspectorBox);
    myScaleSpin->setObjectName("spinScale");
    myScaleSpin->setRange(0.01, 100.0);
    myScaleSpin->setSingleStep(0.1);
    myScaleSpin->setValue(1.0);
    myScaleSpin->setSuffix(" ×");
    myScaleSpin->setButtonSymbols(QAbstractSpinBox::NoButtons);
    myScaleSpin->setAlignment(Qt::AlignCenter);
    myScaleSpin->setMinimumWidth(40);
    myScaleSpin->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
    scaleRow->addWidget(myScaleSpin, 1);
    inspectorLayout->addLayout(scaleRow);

    // Movement Path Section for Selected Prim
    QFrame* pathSep = new QFrame(inspectorBox);
    pathSep->setFrameShape(QFrame::HLine);
    pathSep->setStyleSheet("color: #30363d; margin: 4px 0px;");
    inspectorLayout->addWidget(pathSep);

    QHBoxLayout* pathHeader = new QHBoxLayout();
    QLabel* pathTitle = new QLabel("MOVEMENT PATH", inspectorBox);
    pathTitle->setStyleSheet("font-size: 10px; font-weight: 700; color: #8b949e; letter-spacing: 0.5px;");
    pathHeader->addWidget(pathTitle);
    pathHeader->addStretch();
    myPrimPathInfoLabel = new QLabel("No path", inspectorBox);
    myPrimPathInfoLabel->setStyleSheet("font-size: 10px; color: #8b949e;");
    pathHeader->addWidget(myPrimPathInfoLabel);
    inspectorLayout->addLayout(pathHeader);

    QHBoxLayout* pathButtonsRow = new QHBoxLayout();
    pathButtonsRow->setSpacing(4);

    myPrimDrawPathButton = new QPushButton("✏️ Draw Path", inspectorBox);
    myPrimDrawPathButton->setCheckable(true);
    myPrimDrawPathButton->setEnabled(false);
    myPrimDrawPathButton->setCursor(Qt::PointingHandCursor);
    myPrimDrawPathButton->setToolTip("Sketch 3D movement path for this asset directly in the viewport (Click & drag in 3D)");
    myPrimDrawPathButton->setStyleSheet(
        "QPushButton { background: #161b22; color: #c9d1d9; border: 1px solid #30363d; "
        "border-radius: 5px; padding: 5px; font-size: 11px; font-weight: 600; min-height: 24px; } "
        "QPushButton:hover { background: #21262d; border-color: #58a6ff; color: #ffffff; } "
        "QPushButton:checked { background: #1a365d; border: 1.5px solid #388bfd; color: #58a6ff; font-weight: 700; } "
        "QPushButton:disabled { background: #13171c; color: #484f58; border-color: #21262d; }");
    connect(myPrimDrawPathButton, &QPushButton::toggled, this, &MainWindow::toggleDrawPathForSelectedPrim);
    pathButtonsRow->addWidget(myPrimDrawPathButton, 1);

    myPrimClearPathButton = new QPushButton("✕", inspectorBox);
    myPrimClearPathButton->setFixedWidth(28);
    myPrimClearPathButton->setEnabled(false);
    myPrimClearPathButton->setCursor(Qt::PointingHandCursor);
    myPrimClearPathButton->setToolTip("Clear sketched movement path for this asset");
    myPrimClearPathButton->setStyleSheet(
        "QPushButton { background: #261e22; color: #f87171; border: 1px solid #4c1d24; "
        "border-radius: 5px; font-size: 11px; font-weight: 700; min-height: 24px; } "
        "QPushButton:hover { background: #3f1d24; border-color: #ef4444; } "
        "QPushButton:disabled { background: #13171c; color: #484f58; border-color: #21262d; }");
    connect(myPrimClearPathButton, &QPushButton::clicked, this, &MainWindow::clearPathForSelectedPrim);
    pathButtonsRow->addWidget(myPrimClearPathButton);

    inspectorLayout->addLayout(pathButtonsRow);

    myRemoveAssetButton = new QPushButton("🗑 Remove Selected Prim", inspectorBox);
    myRemoveAssetButton->setObjectName("dangerButton");
    myRemoveAssetButton->setCursor(Qt::PointingHandCursor);
    myRemoveAssetButton->setStyleSheet("QPushButton { background: #2d181e; color: #f85149; border: 1px solid #5a1e27; "
                                       "border-radius: 5px; padding: 5px; font-size: 11px; font-weight: 600; min-height: 24px; } "
                                       "QPushButton:hover { background: #3c1e27; border-color: #f85149; color: #ff7b72; } "
                                       "QPushButton:disabled { background: #13171c; color: #484f58; border-color: #21262d; }");
    myRemoveAssetButton->setEnabled(false);
    inspectorLayout->addWidget(myRemoveAssetButton);
    inspectorLayout->addStretch(1);

    upperLayout->addWidget(inspectorBox);
    upperLayout->addStretch(1);

    upperScroll->setWidget(upperContainer);
    rightLayout->addWidget(upperScroll, 1);

    // Assets Catalog panel placed on right sidebar below Prim Inspector
    QGroupBox* assetsBox = new QGroupBox("ASSETS", rightSidebar);
    assetsBox->setStyleSheet(panelStyle());
    QVBoxLayout* assetsBoxLayout = new QVBoxLayout(assetsBox);
    assetsBoxLayout->setContentsMargins(6, 12, 6, 6);
    assetsBoxLayout->setSpacing(8);

    QHBoxLayout* catHeader = new QHBoxLayout();
    catHeader->setContentsMargins(0, 0, 0, 0);
    catHeader->setSpacing(6);

    QLabel* categoryLabel = new QLabel("CATEGORY", assetsBox);
    categoryLabel->setStyleSheet("font-size: 11px; font-weight: 700; color: #8ad0f5;");
    catHeader->addWidget(categoryLabel);
    catHeader->addStretch();

    QPushButton* refreshBtn = new QPushButton("↻ Refresh", assetsBox);
    refreshBtn->setStyleSheet("QPushButton { font-size: 11px; padding: 2px 7px; border: 1px solid #364452; border-radius: 4px; color: #a0b0c0; background: #1a222a; }"
                              "QPushButton:hover { background: #263544; color: #ffffff; }");
    refreshBtn->setCursor(Qt::PointingHandCursor);
    refreshBtn->setToolTip("Rescan assets/usdc_assets folder and reload categories");
    connect(refreshBtn, &QPushButton::clicked, this, &MainWindow::refreshAssetList);
    catHeader->addWidget(refreshBtn);
    assetsBoxLayout->addLayout(catHeader);

    myAssetCategoryCombo = new QComboBox(assetsBox);
    myAssetCategoryCombo->setStyleSheet(
        "QComboBox { background: #1a222a; color: #f0f4f8; border: 1px solid #364452; "
        "border-radius: 5px; padding: 5px 8px; font-size: 12px; font-weight: 600; min-height: 28px; }"
        "QComboBox::drop-down { border: none; width: 22px; }"
        "QComboBox QAbstractItemView { background: #151b22; color: #f0f4f8; selection-background-color: #2e445a; border: 1px solid #364452; }");
    assetsBoxLayout->addWidget(myAssetCategoryCombo);

    QScrollArea* assetScroll = new QScrollArea(assetsBox);
    assetScroll->setWidgetResizable(true);
    assetScroll->setFrameStyle(QFrame::NoFrame);
    assetScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    assetScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    assetScroll->setStyleSheet("QScrollArea { background: transparent; border: none; }");
    assetScroll->viewport()->setStyleSheet("background: transparent; border: none;");
    QWidget* assetList = new QWidget(assetScroll);
    myAssetListLayout = new QVBoxLayout(assetList);
    myAssetListLayout->setContentsMargins(0, 4, 4, 0);
    myAssetListLayout->setSpacing(6);

    populateAssetEntries();

    assetScroll->setWidget(assetList);
    assetsBoxLayout->addWidget(assetScroll, 1);

    connect(myAssetCategoryCombo, QOverload<int>::of(&QComboBox::currentIndexChanged),
            this, &MainWindow::filterAssetsByCategory);

    rightLayout->addWidget(assetsBox, 1);

    columns->addWidget(rightSidebar);
    root->addLayout(columns, 1);

    // Action Timeline System (multi-track character action timeline)
    myActionTimeline = new ActionTimelineWidget(myEditorPage);
    root->addWidget(myActionTimeline);
    auto* directorRow = new QHBoxLayout();
    myGenerationMode = new QComboBox(myEditorPage); myGenerationMode->addItems({"Auto", "Assisted", "Manual"});
    myGenerationMode->setToolTip("Manual compiles authored actor intent without an LLM call; camera mode is independent");
    directorRow->addWidget(myGenerationMode);
    auto* inspectBrief = new QPushButton("What the AI will see · Check brief", myEditorPage);
    directorRow->addWidget(inspectBrief);
    connect(inspectBrief, &QPushButton::clicked, this, &MainWindow::inspectDirectorBrief);
    myDirectorState = new QLabel(myEditorPage); directorRow->addWidget(myDirectorState, 1);
    myCameraTakes = new QComboBox(myEditorPage); directorRow->addWidget(myCameraTakes);
    auto* duplicateTake = new QPushButton("Save camera take", myEditorPage); directorRow->addWidget(duplicateTake);
    auto* keys = new QPushButton("Camera keys", myEditorPage); directorRow->addWidget(keys);
    connect(keys, &QPushButton::clicked, this, &MainWindow::editCameraKeys);
    myRerenderCameraButton = new QPushButton("Re-render camera", myEditorPage); directorRow->addWidget(myRerenderCameraButton);
    connect(myRerenderCameraButton, &QPushButton::clicked, this, [this]() { renderCameraTakes(false); });
    auto* allTakes = new QPushButton("Render all takes", myEditorPage); directorRow->addWidget(allTakes);
    connect(allTakes, &QPushButton::clicked, this, [this]() { renderCameraTakes(true); });
    for (auto* button : {inspectBrief, duplicateTake, keys, myRerenderCameraButton, allTakes}) {
        button->setStyleSheet("QPushButton { background: #20262e; color: #dce3e9; border: 1px solid #343e4b; border-radius: 5px; padding: 6px 9px; }"
                              "QPushButton:hover { background: #29323d; color: #ffffff; }"
                              "QPushButton:disabled { background: #181d23; color: #84909d; border-color: #303945; }");
    }
    connect(duplicateTake, &QPushButton::clicked, this, [this]() {
        if (!myUsdViewport) return;
        myUsdViewport->bakeCurrentCameraRig();
        auto doc = myUsdViewport->cameraDocument(); auto cameras = doc["cameras"].toObject();
        if (cameras.isEmpty()) return;
        bool ok = false; QString label = QInputDialog::getText(this, "Camera take", "Take name", QLineEdit::Normal, "New camera take", &ok);
        if (!ok || label.trimmed().isEmpty()) return;
        QString id = "cam_" + QString::number(cameras.size()+1); while (cameras.contains(id)) id += "_copy";
        auto camera = cameras[doc["active_camera"].toString()].toObject(); camera["label"] = label;
        cameras[id] = camera; doc["cameras"] = cameras; doc["active_camera"] = id;
        myUsdViewport->pushUndoSnapshot(); myUsdViewport->setCameraDocument(doc); myUsdViewport->bakeCurrentCameraRig(); refreshDirectorState();
    });
    connect(myCameraTakes, &QComboBox::currentIndexChanged, this, [this](int index) {
        if (index < 0 || !myUsdViewport) return;
        auto doc = myUsdViewport->cameraDocument(); const QString id = myCameraTakes->itemData(index).toString();
        if (doc["active_camera"].toString() == id) return;
        myUsdViewport->pushUndoSnapshot(); doc["active_camera"] = id; myUsdViewport->setCameraDocument(doc); myUsdViewport->bakeCurrentCameraRig(); refreshDirectorState();
    });
    root->addLayout(directorRow);
    myBriefLint = new QListWidget(myEditorPage); myBriefLint->setMaximumHeight(100); myBriefLint->setVisible(false); root->addWidget(myBriefLint);
    connect(myBriefLint, &QListWidget::itemClicked, this, [this](QListWidgetItem* item) {
        const auto issue = item->data(Qt::UserRole).toJsonObject();
        const auto range = issue["time_range"].toArray();
        const double time = range.isEmpty() ? issue["time_s"].toDouble() : range[0].toDouble();
        if (myUsdViewport) myUsdViewport->setCurrentFrame(time*24);
        const auto fixes = issue["fixes"].toArray(); if (fixes.isEmpty()) return;
        QMenu menu(this);
        for (const auto& fix : fixes) {
            auto* action = menu.addAction(fix.toString().replace('_', ' '));
            connect(action, &QAction::triggered, this, [this, issue, fix]() {
                auto report = runDirectorTool({"--brief-fix", fix.toString(), "--segment-id", issue["segment"].toString(), "--path-id", issue["path"].toString()});
                if (report.contains("brief")) { applyDirectorBrief(report["brief"].toObject()); inspectDirectorBrief(); }
            });
        }
        menu.exec(QCursor::pos());
    });
    connect(myActionTimeline, &ActionTimelineWidget::targetPickRequested, this, [this]() {
        myPickingBriefTarget = true; myUsdViewport->stopDrawingPath();myUsdViewport->setTool(PaintingTool::Select);
        myCanvasStatus->setText("Click an actor or object to use as the action target");
    });
    connect(myActionTimeline, &ActionTimelineWidget::drawSegmentPathRequested, this, [this](const QString&, const QString& path) {
        myUsdViewport->startDrawingPath("/World/Paths/" + path);
    });
    connect(myActionTimeline, &ActionTimelineWidget::segmentSelected, this, [this](const ActionSegment& seg) {
        if (!seg.pathId.isEmpty()) myUsdViewport->selectPrim("/World/Paths/"+seg.pathId);
    });
    auto* directorDebounce = new QTimer(this); directorDebounce->setSingleShot(true); directorDebounce->setInterval(500);
    connect(directorDebounce, &QTimer::timeout, this, &MainWindow::refreshDirectorState);
    connect(myScenePrompt, &QPlainTextEdit::textChanged, directorDebounce, QOverload<>::of(&QTimer::start));
    connect(myUsdViewport, &UsdViewportWidget::userEditsChanged, this, [directorDebounce](int) { directorDebounce->start(); });

    connect(myActionTimeline, &ActionTimelineWidget::timeScrubbed, this, [this](double t) {
        if (myUsdViewport) {
            myUsdViewport->setCurrentFrame(t * 24.0);
        }
    });
    connect(myActionTimeline, &ActionTimelineWidget::durationChanged, this, [this](double dur) {
        if (myCurrentPanel >= 0 && myCurrentPanel < storyboardPanels().size()) {
            storyboardPanels()[myCurrentPanel].durationS = dur;
        }
        saveShotActions(myCurrentScene, myCurrentPanel);
    });
    connect(myActionTimeline, &ActionTimelineWidget::syncPromptRequested, this, [this](const QString&) { inspectDirectorBrief(); });
    connect(myActionTimeline, &ActionTimelineWidget::actionsChanged, this, [this]() {
        if (mySyncingTimelineMovementDistances) return;
        syncTimelineMovementDistances();
        for (const auto& seg : myActionTimeline->actions()) if (seg.id == "render_only_camera" && !myUsdViewport->cameraDocument().isEmpty()) {
            auto doc = myUsdViewport->cameraDocument(); auto cameras = doc["cameras"].toObject(); auto cid = doc["active_camera"].toString();
            auto camera = cameras[cid].toObject(); auto path = camera["path"].toObject();
            const QJsonArray range{seg.startS, seg.endS};
            if (path["time_range"].toArray() != range) { path["time_range"] = range; camera["path"] = path; cameras[cid] = camera; doc["cameras"] = cameras;
                myUsdViewport->setCameraDocument(doc); myUsdViewport->bakeCurrentCameraRig(); }
        }
        saveShotActions(myCurrentScene, myCurrentPanel);
        updateEditorUndoButtonState();
    });
    connect(myActionTimeline, &ActionTimelineWidget::undoSnapshotPushed, this, [this]() {
        myGlobalUndoStack.append(UndoActionSource::Timeline);
        if (myGlobalUndoStack.size() > 100) myGlobalUndoStack.removeFirst();
        updateEditorUndoButtonState();
    });
    connect(myActionTimeline, &ActionTimelineWidget::undoStateChanged, this, &MainWindow::updateEditorUndoButtonState);
    connect(myActionTimeline, &ActionTimelineWidget::actorSelected, this, [this](const QString& actorPath) {
        if (myUsdViewport && !actorPath.isEmpty() && myUsdViewport->selectedPrimPath() != actorPath) {
            myUsdViewport->selectPrim(actorPath);
        }
    });
    connect(myUsdViewport, &UsdViewportWidget::playbackFrameChanged, this, [this](double f) {
        if (myActionTimeline) {
            myActionTimeline->setCurrentTime(f / 24.0);
        }
    });
    connect(myUsdViewport, &UsdViewportWidget::primPathChanged, this, [this](const QString&) {
        updatePrimPathControls();
        syncTimelineMovementDistances();
        saveShotActions(myCurrentScene, myCurrentPanel);
    });
    connect(myUsdViewport, &UsdViewportWidget::drawPathModeChanged, this, [this](bool active, const QString& prim) {
        Q_UNUSED(active);
        Q_UNUSED(prim);
        updatePrimPathControls();
        updateCameraInspectorUi();
    });

    connect(myUsdViewport, &UsdViewportWidget::selectionChanged, this, [this](const QString& primPath) {
        if (myPickingBriefTarget && !primPath.isEmpty()) {
            myPickingBriefTarget = false; QString target = primPath.section('/', -1);
            for (const auto& prim : myUsdViewport->primList()) if (prim.path == primPath && !prim.actorId.isEmpty()) {target=prim.actorId;break;}
            myActionTimeline->setPickedTarget(target);return;
        }
        updateSelectionPanel();
        syncLayers();
        if (myActionTimeline && !primPath.isEmpty()) {
            myActionTimeline->selectActorByPath(primPath);
        }
    });
    connect(myUsdViewport, &UsdViewportWidget::undoSnapshotPushed, this, [this]() {
        myGlobalUndoStack.append(UndoActionSource::Viewport);
        if (myGlobalUndoStack.size() > 100) myGlobalUndoStack.removeFirst();
        updateEditorUndoButtonState();
    });
    connect(myUsdViewport, &UsdViewportWidget::stageChanged, this, [this]() {
        syncLayers();
        updateSelectionPanel();
    });
    connect(myUsdViewport, &UsdViewportWidget::userEditsChanged, this, [this](int) {
        syncLayers();
        updateSelectionPanel();
        if (myCurrentPanel < 0) {
            updateEditorModeUi();
        }
        updateEditorUndoButtonState();
    });
    connect(myUsdViewport, &UsdViewportWidget::statusMessageChanged, this, [this](const QString& msg) {
        if (myCanvasStatus) {
            myCanvasStatus->setText(msg);
        }
    });
    connect(myUsdViewport, &UsdViewportWidget::viewModeChanged, this, [this](ViewMode mode) {
        updateCameraInspectorUi();
        if (myCanvasStatus) {
            if (mode == ViewMode::CameraViewfinder) {
                myCanvasStatus->setText("Camera Viewfinder active (Aspect Cropped) • Press Tab to return to 3D World Editor");
            } else {
                myCanvasStatus->setText("3D World Editor active • Press Tab to enter Camera Viewfinder");
            }
        }
    });
    connect(myLayers, &QListWidget::currentRowChanged, this, [this](int row) {
        if (myUsdViewport) {
            QListWidgetItem* item = myLayers->item(row);
            if (item) {
                myUsdViewport->selectPrim(item->data(Qt::UserRole).toString());
            }
        }
    });
    connect(myLayers, &QListWidget::itemChanged, this, [this](QListWidgetItem* item) {
        if (!item || !myUsdViewport) return;
        myUsdViewport->setPrimVisible(item->data(Qt::UserRole).toString(), item->checkState() == Qt::Checked);
    });
    connect(myScaleSpin, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateSelectionScale);
    connect(myPositionX, &QDoubleSpinBox::valueChanged, this, &MainWindow::positionCoordinateChanged);
    connect(myPositionY, &QDoubleSpinBox::valueChanged, this, &MainWindow::positionCoordinateChanged);
    connect(myPositionZ, &QDoubleSpinBox::valueChanged, this, &MainWindow::positionCoordinateChanged);
    connect(myRotationX, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateSelectionRotation);
    connect(myRotationY, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateSelectionRotation);
    connect(myRotationZ, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateSelectionRotation);
    connect(myRemoveAssetButton, &QPushButton::clicked, this, &MainWindow::removeSelectedAsset);

    setStyleSheet(
        "QMainWindow, #workspace { background: #15191e; color: #e3e8ed; }"
        "#projectPage, #scenesPage, #bookPage, #editorPage { background: #15191e; }"
        "QLabel { color: #dce3e9; }"
        "#appTitle { font-size: 20px; font-weight: 700; color: #f5f7f9; }"
        "#brandMark { background: #c85c4b; color: white; border-radius: 8px; font-size: 20px; font-weight: 700; }"
        "#projectIcon { background: #304252; color: #f0c47a; border-radius: 10px; font-size: 26px; }"
        "#projectCard, #storyboardCard, #emptyStoryboardCard { background: #1b2027; border: 1px solid #303945; border-radius: 10px; }"
        "#projectCard:hover, #storyboardCard:hover { border-color: #607f98; }"
        "#cardTitle { color: #f0f3f6; font-size: 14px; font-weight: 700; }"
        "#mutedText { color: #82909d; font-size: 11px; }"
        "#sectionLabel { color: #a9b3bf; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }"
        "QDoubleSpinBox, QLineEdit, QComboBox { background: #20262e; border: 1px solid #343e4b; border-radius: 4px; padding: 5px; color: #e3e8ed; }"
        "QDoubleSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled { background: #13171c; color: #484f58; border-color: #21262d; }"
        "QDoubleSpinBox QLineEdit { background: transparent; color: inherit; border: none; }"
        "QDoubleSpinBox QLineEdit:disabled { background: transparent; color: #484f58; border: none; }"
        "#primaryButton { background: #c85c4b; color: white; border: none; border-radius: 6px; padding: 9px 14px; font-weight: 700; }"
        "#primaryButton:hover { background: #d56b59; }"
        "#renderButton { background: #d49345; color: #211b14; border: none; border-radius: 6px; padding: 9px 18px; font-weight: 700; }"
        "#renderButton:hover { background: #e2a255; }"
        "#quietButton, #dangerButton { background: #20262e; color: #aeb9c4; border: 1px solid #343e4b; border-radius: 5px; padding: 7px; }"
        "#dangerButton { color: #e38e83; }"
        "#panelDeleteButton { background: #2a2023; color: #e38e83; border: 1px solid #704047; border-radius: 5px; font-size: 18px; font-weight: 700; }"
        "#panelDeleteButton:hover { background: #63343a; color: white; }"
        "#storyboardPanelButton, #emptyPanelButton { background: #20262e; color: #aeb9c4; border: 1px dashed #465464; border-radius: 7px; font-size: 14px; }"
        "#storyboardPanelButton:hover, #emptyPanelButton:hover { background: #293743; color: white; border-color: #73b5da; }"
        "#storyboardPanelButton { border-style: solid; }"
        "#layersList { background: #1b2027; border: none; border-radius: 5px; padding: 4px; }"
        "#layersList::item { padding: 8px 6px; color: #bac4ce; border-radius: 4px; }"
        "#layersList::item:selected { background: #304252; color: white; }"
        "#usdViewport { background: #1b2027; border: 1px solid #303945; border-radius: 8px; }"
        "QScrollArea { background: #15191e; border: none; }"
        "QScrollArea > QWidget > QWidget { background: #15191e; }");

    syncLayers();
    updateSelectionPanel();
    loadProjectScenesFromDisk();
    rebuildStoryboardScenes();
    rebuildStoryboardBook();
    if (!myStoryboardScenes.isEmpty() && myStoryboardScenes.first().configured) {
        showStoryboardScenes();
    } else {
        myViewStack->setCurrentWidget(myProjectPage);
    }
    // The 3D editor opens from a scene or storyboard shot.
    myCanvasStatus->setText("USD Hydra Storm Viewport  •  Orbit with middle-drag  •  Pick & move prims in user_edits.usda");

    // Watch task_queue/llm_response.json so when the IDE LLM writes JSON function calls,
    // PainterSpace automatically executes them and compiles/reloads the stage itself.
    auto* llmResponseTimer = new QTimer(this);
    llmResponseTimer->setInterval(750);
    connect(llmResponseTimer, &QTimer::timeout, this, &MainWindow::pollAgentLlmResponse);
    llmResponseTimer->start();
}

void MainWindow::loadProjectScenesFromDisk() {
    myStoryboardScenes.clear();
    const QString projectRoot = painterSpaceProjectRoot();
    const QJsonObject projectMeta = readJsonFile(QDir(projectRoot).filePath("project.json"));
    if (!projectMeta.value("title").toString().trimmed().isEmpty()) {
        myProjectTitle = projectMeta.value("title").toString().trimmed();
    } else {
        myProjectTitle = QStringLiteral("Storyboard Project");
    }

    int maxScenes = 6;
    const QDir scenesRootDir(QDir(projectRoot).filePath("scenes"));
    if (scenesRootDir.exists()) {
        const QStringList sceneDirs = scenesRootDir.entryList(QStringList() << "scene_*", QDir::Dirs | QDir::NoDotAndDotDot);
        for (const QString& d : sceneDirs) {
            bool ok = false;
            const int num = d.mid(6).toInt(&ok);
            if (ok && num > maxScenes) maxScenes = num;
        }
    }

    for (int index = 0; index < maxScenes; ++index) {
        StoryboardScene scene;
        scene.name = QString("Scene %1").arg(index + 1);
        const QString sceneFolder = QString("scenes/scene_%1").arg(index + 1, 2, 10, QChar('0'));
        const QDir sceneDir(QDir(projectRoot).filePath(sceneFolder));
        int maxShotNumber = kInitialStoryboardSlots;
        const QStringList shotDirs = sceneDir.entryList(QStringList() << "shot_*", QDir::Dirs | QDir::NoDotAndDotDot);
        for (const QString& shotDirName : shotDirs) {
            bool ok = false;
            const int number = shotDirName.mid(QStringLiteral("shot_").size()).toInt(&ok);
            if (ok) maxShotNumber = std::max(maxShotNumber, number);
        }
        scene.panels.resize(maxShotNumber);
        const QString sceneUsda = sceneDir.filePath("scene.usda");
        const QString sceneJson = sceneDir.filePath("scene.json");
        const QString worldStateJson = sceneDir.filePath("world_state.json");

        if (QFileInfo::exists(sceneUsda)) {
            scene.usdStagePath = sceneUsda;
            scene.loadFromDisk = true;
            scene.configured = true;
        }
        if (QFileInfo::exists(sceneJson)) {
            const QJsonObject meta = readJsonFile(sceneJson);
            if (!meta.value("name").toString().trimmed().isEmpty()) {
                scene.name = meta.value("name").toString().trimmed();
            }
            scene.prompt = meta.value("prompt").toString().trimmed();
            const QJsonArray revisions = meta.value("revisions").toArray();
            if (!revisions.isEmpty()) {
                scene.prompt = revisions.last().toString(scene.prompt).trimmed();
            }
            if (meta.contains("configured")) {
                scene.configured = meta.value("configured").toBool() || !scene.prompt.isEmpty();
            } else if (!scene.prompt.isEmpty()) {
                scene.configured = true;
            }
        }
        if (QFileInfo::exists(sceneDir.filePath("thumbnail.jpg"))) {
            scene.render = QImage(sceneDir.filePath("thumbnail.jpg"));
        } else if (QFileInfo::exists(sceneDir.filePath("world_state_topdown.png"))) {
            scene.render = QImage(sceneDir.filePath("world_state_topdown.png"));
        }
        if (QFileInfo::exists(worldStateJson)) {
            scene.loadFromDisk = true;
            scene.configured = true;
        }

        for (int panelIdx = 0; panelIdx < scene.panels.size(); ++panelIdx) {
            const QString shotRelDir = QString("%1/shot_%2").arg(sceneFolder).arg(panelIdx + 1, 3, 10, QChar('0'));
            const QDir shotDir(QDir(projectRoot).filePath(shotRelDir));
            const QString shotUsda = shotDir.filePath("shot.usda");
            const QString shotJson = shotDir.filePath("shot.json");
            if (!QFileInfo::exists(shotUsda) && !QFileInfo::exists(shotJson)) continue;
            StoryboardPanel& panel = scene.panels[panelIdx];
            panel.active = true;
            panel.name = QString("Shot %1").arg(panelIdx + 1);
            if (QFileInfo::exists(shotUsda)) panel.usdStagePath = shotUsda;
            QFile metaFile(shotJson);
            if (metaFile.open(QIODevice::ReadOnly)) {
                const QJsonObject meta = QJsonDocument::fromJson(metaFile.readAll()).object();
                panel.prompt = meta.value("prompt").toString();
                const QJsonArray revisions = meta.value("revisions").toArray();
                if (!revisions.isEmpty()) {
                    panel.prompt = revisions.last().toString(panel.prompt);
                }
            }
            const QString thumbPath = shotDir.filePath("thumbnail.jpg");
            const QString firstFramePath = shotDir.filePath("frames/frame.000.png");
            if (QFileInfo::exists(thumbPath)) {
                panel.render = QImage(thumbPath);
            } else if (QFileInfo::exists(firstFramePath)) {
                panel.render = QImage(firstFramePath);
            }
            if (scene.render.isNull() && !panel.render.isNull()) {
                scene.render = panel.render;
            }
            scene.configured = true;
        }
        myStoryboardScenes.push_back(scene);
    }
}

void MainWindow::showProjectExplorer() {
    if (myViewStack) myViewStack->setCurrentWidget(myProjectPage);
}

void MainWindow::createStoryboardProject() {
    const QString enteredTitle = myNewProjectNameEdit ? myNewProjectNameEdit->text().trimmed() : QString();
    myProjectTitle = enteredTitle.isEmpty() ? QStringLiteral("New Storyboard Project") : enteredTitle;
    myStoryboardScenes.clear();
    for (int index = 0; index < 6; ++index) {
        StoryboardScene scene;
        scene.name = QString("Scene %1").arg(index + 1);
        scene.configured = false;
        scene.loadFromDisk = false;
        scene.panels.resize(kInitialStoryboardSlots);
        myStoryboardScenes.push_back(scene);
    }
    myCurrentScene = -1;
    myCurrentPanel = -1;
    myLastEditedPanel = -1;
    showStoryboardScenes();
}

void MainWindow::openStoryboardProject() {
    loadProjectScenesFromDisk();
    showStoryboardScenes();
}

void MainWindow::addStoryboardScene() {
    const int newSceneIndex = myStoryboardScenes.size();
    StoryboardScene scene;
    scene.name = QString("Scene %1").arg(newSceneIndex + 1);
    scene.configured = false;
    scene.loadFromDisk = false;
    scene.panels.resize(kInitialStoryboardSlots);

    // Persist the new scene directory and metadata on disk so it survives app restarts
    const QString projectRoot = painterSpaceProjectRoot();
    const QString sceneId = QString("scene_%1").arg(newSceneIndex + 1, 2, 10, QChar('0'));
    const QDir sceneDir(QDir(projectRoot).filePath(QString("scenes/%1").arg(sceneId)));
    QDir().mkpath(sceneDir.absolutePath());
    const QString sceneJsonPath = sceneDir.filePath("scene.json");
    if (!QFileInfo::exists(sceneJsonPath)) {
        const QJsonObject meta{
            {"scene_id", sceneId},
            {"name", scene.name},
            {"stage", QStringLiteral("scene.usda")},
            {"prompt", QString()},
            {"configured", false}
        };
        QSaveFile out(sceneJsonPath);
        if (out.open(QIODevice::WriteOnly | QIODevice::Text)) {
            out.write(QJsonDocument(meta).toJson(QJsonDocument::Indented));
            out.commit();
        }
    }

    myStoryboardScenes.push_back(scene);
    rebuildStoryboardScenes();
}

void MainWindow::showStoryboardScenes() {
    if (myScenesTitle) {
        myScenesTitle->setText(myProjectTitle + QStringLiteral(" — Scenes"));
    }
    rebuildStoryboardScenes();
    if (myViewStack) myViewStack->setCurrentWidget(myScenesPage);
}



QString MainWindow::ensureSceneStageFile(int sceneIndex) const {
    const QString projectRoot = painterSpaceProjectRoot();
    const QString sceneFolder = QString("scenes/scene_%1").arg(sceneIndex + 1, 2, 10, QChar('0'));
    const QDir sceneDir(QDir(projectRoot).filePath(sceneFolder));
    QDir().mkpath(sceneDir.absolutePath());
    const QString sceneStagePath = sceneDir.filePath("scene.usda");
    if (!QFileInfo::exists(sceneStagePath)) {
        QSaveFile out(sceneStagePath);
        const QByteArray bytes = defaultSceneUsdaSource();
        if (out.open(QIODevice::WriteOnly | QIODevice::Text)) {
            out.write(bytes);
            out.commit();
        }
    }
    return sceneStagePath;
}

QString MainWindow::ensureShotStageFile(int sceneIndex, int panelIndex) const {
    ensureSceneStageFile(sceneIndex);
    const QString projectRoot = painterSpaceProjectRoot();
    const QString sceneFolder = QString("scenes/scene_%1").arg(sceneIndex + 1, 2, 10, QChar('0'));
    const QString shotFolder = QString("%1/shot_%2").arg(sceneFolder).arg(panelIndex + 1, 3, 10, QChar('0'));
    const QDir shotDir(QDir(projectRoot).filePath(shotFolder));
    QDir().mkpath(shotDir.absolutePath());
    const QString shotStagePath = shotDir.filePath("shot.usda");
    if (!QFileInfo::exists(shotStagePath)) {
        QSaveFile out(shotStagePath);
        const QByteArray bytes = defaultShotUsdaSource();
        if (out.open(QIODevice::WriteOnly | QIODevice::Text)) {
            out.write(bytes);
            out.commit();
        }
    }
    return shotStagePath;
}

void MainWindow::openStoryboardScene(int sceneIndex) {
    if (sceneIndex < 0 || sceneIndex >= myStoryboardScenes.size()) return;
    myCurrentScene = sceneIndex;
    const StoryboardScene& scene = myStoryboardScenes.at(sceneIndex);
    if (scene.configured) {
        showStoryboardBook();
    } else {
        openSceneSetup(sceneIndex);
    }
}

void MainWindow::openSceneSetup(int sceneIndex) {
    if (sceneIndex < 0 || sceneIndex >= myStoryboardScenes.size()) return;
    myCurrentScene = sceneIndex;
    myCurrentPanel = -1;
    myLastEditedPanel = -1;
    for (int idx = 0; idx < storyboardPanels().size(); ++idx) {
        if (storyboardPanels().at(idx).active) {
            myLastEditedPanel = idx;
            break;
        }
    }

    StoryboardScene& scene = myStoryboardScenes[sceneIndex];
    const QString sceneStagePath = ensureSceneStageFile(sceneIndex);
    scene.usdStagePath = sceneStagePath;

    if (myUsdViewport) {
        myUsdViewport->openStage(sceneStagePath, true);
    }

    if (myScenePrompt) {
        myScenePrompt->setPlainText(scene.prompt);
    }
    updateEditorModeUi();
    syncLayers();
    updateSelectionPanel();
    updateEditorUndoButtonState();
    myCanvasStatus->setText(QString("Step 1: Describe %1 via LLM prompt or USD 3D editor, then click 'Continue to Shots →'")
                                .arg(scene.name));
    if (myViewStack) myViewStack->setCurrentWidget(myEditorPage);
}

void MainWindow::confirmSceneSetupAndOpenShots() {
    if (myCurrentScene < 0 || myCurrentScene >= myStoryboardScenes.size()) return;
    StoryboardScene& scene = myStoryboardScenes[myCurrentScene];
    const QString prompt = myScenePrompt ? myScenePrompt->toPlainText().trimmed() : QString();
    const bool has3dEditorInput = (myUsdViewport && myUsdViewport->hasStage());
    if (prompt.isEmpty() && !has3dEditorInput && !scene.configured) {
        QMessageBox::information(
            this,
            "Describe the scene first",
            "Please describe this scene before moving into individual shots.\n\n"
            "You can either:\n"
            "  • Enter a scene description in the LLM prompt box above, OR\n"
            "  • Add reference assets / paint in the 3D editor below.");
        if (myScenePrompt) myScenePrompt->setFocus();
        return;
    }

    if (myScenePrompt) {
        scene.prompt = prompt;
    }

    saveCurrentSceneEdits();
    showStoryboardBook();
}

void MainWindow::saveCurrentSceneEdits() {
    if (myCurrentScene < 0 || myCurrentScene >= myStoryboardScenes.size() || !myUsdViewport) return;
    StoryboardScene& scene = myStoryboardScenes[myCurrentScene];

    if (myScenePrompt) {
        scene.prompt = myScenePrompt->toPlainText().trimmed();
    }

    QString errorMsg;
    QString snapshotFile;
    const int snapVer = myUsdViewport->snapshotSceneForExistingShots(&snapshotFile);
    const bool ok = myUsdViewport->saveSceneEdits(&errorMsg);
    if (!ok) {
        QMessageBox::warning(this, "Save Scene Edits", QString("Could not save scene edits:\n%1").arg(errorMsg));
        return;
    }

    const QString sceneStagePath = ensureSceneStageFile(myCurrentScene);
    const QDir sceneDir = QFileInfo(sceneStagePath).dir();

    const QImage renderedView = myUsdViewport->renderCurrentView();
    if (!renderedView.isNull()) {
        scene.render = renderedView;
        renderedView.save(sceneDir.filePath("thumbnail.jpg"), "JPG", 90);
    }

    // Save scene.json metadata sidecar
    {
        const QString sceneJsonPath = sceneDir.filePath("scene.json");
        QJsonObject meta = readJsonFile(sceneJsonPath);
        meta["scene_id"] = QString("scene_%1").arg(myCurrentScene + 1, 2, 10, QChar('0'));
        meta["stage"] = QStringLiteral("scene.usda");
        meta["prompt"] = scene.prompt;
        meta["configured"] = true;
        meta["has_3d_editor_layout"] = true;
        if (snapVer > 0) {
            QJsonArray snapshots = meta.value("snapshots").toArray();
            QJsonObject snapObj;
            snapObj["version"] = snapVer;
            snapObj["file"] = snapshotFile;
            snapshots.append(snapObj);
            meta["snapshots"] = snapshots;
        }
        QSaveFile metaOut(sceneJsonPath);
        if (metaOut.open(QIODevice::WriteOnly | QIODevice::Text)) {
            metaOut.write(QJsonDocument(meta).toJson(QJsonDocument::Indented));
            metaOut.commit();
        }
    }

    // Rebuild world state so future shot planning knows the updated layout
    const QString projectRoot = painterSpaceProjectRoot();
    const QString worldStateScript = QDir(projectRoot).filePath("tools/world_state.py");
    if (QFileInfo::exists(worldStateScript)) {
        QProcess proc;
        QString pythonBin = QDir(projectRoot).filePath(".venv/bin/python");
        if (!QFileInfo::exists(pythonBin)) pythonBin = QStringLiteral("python3");
        proc.start(pythonBin, QStringList() << worldStateScript << "build" << sceneStagePath);
        proc.waitForFinished(10000);
    }

    scene.configured = true;
    scene.loadFromDisk = true;
    scene.usdStagePath = sceneStagePath;

    rebuildStoryboardScenes();
    rebuildStoryboardBook();
    updateEditorModeUi();

    QString statusMsg = QStringLiteral("✓ Main scene edits saved to scene.usda!");
    if (snapVer > 0) {
        statusMsg += QString(" Existing shots frozen on %1; all new shots inherit this scene.").arg(snapshotFile);
    } else {
        statusMsg += QStringLiteral(" All new shots inherit this scene.");
    }
    myUsdViewport->setGenerationStatus(statusMsg, false, false);
    if (myCanvasStatus) {
        myCanvasStatus->setText(statusMsg);
    }
    syncLayers();
    updateSelectionPanel();
}

void MainWindow::updateEditorModeUi() {
    const bool isSceneMode = (myCurrentPanel < 0);
    const bool agentMode = qgetenv("OPENAI_API_KEY").isEmpty();
    const bool isConfigured = (myCurrentScene >= 0 && myCurrentScene < myStoryboardScenes.size() && myStoryboardScenes.at(myCurrentScene).configured);

    if (isSceneMode) {
        if (myEditorBackButton) {
            myEditorBackButton->setText(isConfigured ? "← Shots" : "← Scenes");
            myEditorBackButton->setToolTip(isConfigured ? "Return to shots grid" : "Return to scenes grid");
        }
        if (myEditorEditSceneButton) myEditorEditSceneButton->setVisible(false);
        if (myEditorTitle) myEditorTitle->setVisible(false);
        if (myEditorModeBadge) myEditorModeBadge->setVisible(false);
        if (myPromptModeLabel) myPromptModeLabel->setVisible(false);
        if (myPromptHintLabel) myPromptHintLabel->setVisible(false);
        if (myEditorPlayClipButton) myEditorPlayClipButton->setVisible(false);

        const int editsCount = (myUsdViewport ? myUsdViewport->userEditedPrimCount() : 0);

        if (myEditorSaveSceneButton) {
            myEditorSaveSceneButton->setVisible(true);
            if (editsCount > 0) {
                myEditorSaveSceneButton->setText(QString("Save Scene (%1)").arg(editsCount));
            } else {
                myEditorSaveSceneButton->setText(QStringLiteral("Save Scene"));
            }
        }

        if (myEditorPrimaryActionButton) {
            if (isConfigured) {
                myEditorPrimaryActionButton->setText(editsCount > 0 ? "Save & Return to Shots  →" : "Return to Shots  →");
            } else {
                myEditorPrimaryActionButton->setText(editsCount > 0 ? QString("Save Scene & Open Shots (%1)  →").arg(editsCount) : "Save Scene & Open Shots  →");
            }
        }
        if (myScenePrompt) {
            myScenePrompt->setPlaceholderText("Describe scene setting, lighting, and baseline objects…");
        }
        if (myGenerateUsdButton) {
            myGenerateUsdButton->setText(agentMode ? "✦  Queue Scene" : "✦  Generate Scene");
            myGenerateUsdButton->setToolTip(agentMode ? "Queue scene description for Gemini" : "Generate scene USD");
        }
    } else {
        if (myEditorBackButton) {
            myEditorBackButton->setText("← Shots");
            myEditorBackButton->setToolTip("Return to shots grid");
        }
        if (myEditorSaveSceneButton) myEditorSaveSceneButton->setVisible(false);
        if (myEditorTitle) myEditorTitle->setVisible(false);
        if (myEditorModeBadge) myEditorModeBadge->setVisible(false);
        if (myPromptModeLabel) myPromptModeLabel->setVisible(false);
        if (myPromptHintLabel) myPromptHintLabel->setVisible(false);
        if (myEditorPlayClipButton) myEditorPlayClipButton->setVisible(false);

        if (myEditorEditSceneButton) {
            myEditorEditSceneButton->setVisible(true);
            myEditorEditSceneButton->setText("✎  Edit Main Scene");
            myEditorEditSceneButton->setToolTip("Open and edit the shared main scene (scene.usda)");
        }
        if (myEditorPrimaryActionButton) {
            myEditorPrimaryActionButton->setText("Save Shot");
        }
        if (myScenePrompt) {
            myScenePrompt->setPlaceholderText("Describe this shot (actions, camera, atmosphere)…");
        }
        if (myGenerateUsdButton) {
            myGenerateUsdButton->setText(agentMode ? "✦  Queue Shot" : "✦  Generate Shot");
            myGenerateUsdButton->setToolTip(agentMode ? "Queue shot prompt for Gemini" : "Generate shot USD");
        }
    }
}

QVector<MainWindow::StoryboardPanel>& MainWindow::storyboardPanels() {
    return myStoryboardScenes[myCurrentScene].panels;
}

const QVector<MainWindow::StoryboardPanel>& MainWindow::storyboardPanels() const {
    return myStoryboardScenes.at(myCurrentScene).panels;
}

void MainWindow::showStoryboardBook() {
    if (myCurrentScene >= 0 && myCurrentScene < myStoryboardScenes.size()) {
        if (myCurrentPanel >= 0 && myCurrentPanel < storyboardPanels().size()) {
            if (myScenePrompt) storyboardPanels()[myCurrentPanel].prompt = myScenePrompt->toPlainText();
        }
    }
    rebuildStoryboardBook();
    if (myViewStack) myViewStack->setCurrentWidget(myBookPage);
}

void MainWindow::startStoryboardPanel() {
    if (myCurrentScene < 0) return;
    for (int index = 0; index < storyboardPanels().size(); ++index) {
        if (!storyboardPanels().at(index).active) {
            startStoryboardPanelAt(index);
            return;
        }
    }
    const int index = storyboardPanels().size();
    storyboardPanels().resize(index + 1);
    startStoryboardPanelAt(index);
}

void MainWindow::startStoryboardPanelAt(int panelIndex) {
    if (myCurrentScene < 0 || panelIndex < 0 || panelIndex >= storyboardPanels().size()) return;
    StoryboardPanel& panel = storyboardPanels()[panelIndex];
    panel.active = true;
    panel.name = QString("Shot %1").arg(panelIndex + 1);
    panel.render = QImage();
    panel.prompt.clear();
    panel.usdStagePath = ensureShotStageFile(myCurrentScene, panelIndex);
    myCurrentPanel = panelIndex;
    myLastEditedPanel = myCurrentPanel;

    if (myUsdViewport) {
        myUsdViewport->openStage(panel.usdStagePath);
    }
    if (myScenePrompt) myScenePrompt->clear();
    loadShotActions(myCurrentScene, panelIndex);
    updateEditorModeUi();
    syncLayers();
    updateSelectionPanel();
    myCanvasStatus->setText(QString("Editing %1  •  Composed USD stage (user_edits.usda over shot.usda over scene.usda)")
                                .arg(storyboardPanels()[myCurrentPanel].name));
    if (myViewStack) myViewStack->setCurrentWidget(myEditorPage);
}

void MainWindow::openStoryboardPanel(int panelIndex) {
    if (myCurrentScene < 0 || panelIndex < 0 || panelIndex >= storyboardPanels().size() || !storyboardPanels().at(panelIndex).active) return;
    myCurrentPanel = panelIndex;
    StoryboardPanel& panel = storyboardPanels()[panelIndex];
    if (panel.usdStagePath.isEmpty() || !QFileInfo::exists(panel.usdStagePath)) {
        panel.usdStagePath = ensureShotStageFile(myCurrentScene, panelIndex);
    }

    if (myUsdViewport) {
        myUsdViewport->openStage(panel.usdStagePath);
    }
    if (myScenePrompt) myScenePrompt->setPlainText(panel.prompt);
    myLastEditedPanel = panelIndex;
    loadShotActions(myCurrentScene, panelIndex);
    updateEditorModeUi();
    syncLayers();
    updateSelectionPanel();
    updateEditorUndoButtonState();
    myCanvasStatus->setText(QString("Editing %1  •  Composed USD stage (%2)")
                                .arg(panel.name, QFileInfo(panel.usdStagePath).fileName()));
    if (myViewStack) myViewStack->setCurrentWidget(myEditorPage);
}

void MainWindow::deleteStoryboardPanel(int panelIndex) {
    if (myCurrentScene < 0 || panelIndex < 0 || panelIndex >= storyboardPanels().size() || !storyboardPanels().at(panelIndex).active) return;
    const QString projectRoot = painterSpaceProjectRoot();
    const QString sceneFolder = QString("scenes/scene_%1").arg(myCurrentScene + 1, 2, 10, QChar('0'));
    const QDir sceneDir(QDir(projectRoot).filePath(sceneFolder));
    const QString shotFolder = QString("shot_%1").arg(panelIndex + 1, 3, 10, QChar('0'));
    const QString shotPath = sceneDir.filePath(shotFolder);
    if (QDir(shotPath).exists()) {
        const QString archivePath = sceneDir.filePath(".deleted_shots");
        const QString archivedShot = QDir(archivePath).filePath(
            shotFolder + "_" + QUuid::createUuid().toString(QUuid::WithoutBraces));
        if (!QDir().mkpath(archivePath) || !QDir().rename(shotPath, archivedShot)) {
            QMessageBox::warning(this, "Could not delete shot",
                                 QString("Could not move %1 out of the shots grid.").arg(shotPath));
            return;
        }
    }
    storyboardPanels()[panelIndex] = StoryboardPanel();
    if (myCurrentPanel == panelIndex) myCurrentPanel = -1;
    if (myLastEditedPanel == panelIndex) {
        myLastEditedPanel = -1;
        for (int index = 0; index < storyboardPanels().size(); ++index) {
            if (storyboardPanels().at(index).active) {
                myLastEditedPanel = index;
                break;
            }
        }
    }
    while (storyboardPanels().size() > kInitialStoryboardSlots && !storyboardPanels().last().active) {
        storyboardPanels().removeLast();
    }
    rebuildStoryboardBook();
}

void MainWindow::rebuildStoryboardBook() {
    if (!myBookGrid || myCurrentScene < 0 || myCurrentScene >= myStoryboardScenes.size()) return;
    while (QLayoutItem* item = myBookGrid->takeAt(0)) {
        if (QWidget* widget = item->widget()) widget->deleteLater();
        delete item;
    }

    const StoryboardScene& currentScene = myStoryboardScenes.at(myCurrentScene);
    if (myBookTitle) myBookTitle->setText(currentScene.name + " — Individual Shots");
    if (myBookSceneSummary) {
        if (!currentScene.prompt.isEmpty()) {
            myBookSceneSummary->setText(QString("Scene Description: \"%1\"  •  All shots inherit this scene's 3D layout and USD layer.")
                                            .arg(currentScene.prompt));
        } else {
            myBookSceneSummary->setText("Scene configured via 3D Editor  •  Click '✎ Edit Scene Input' above to update the scene prompt or 3D layout.");
        }
    }
    if (myLastEditedPanel >= 0 && myLastEditedPanel < storyboardPanels().size() &&
        storyboardPanels().at(myLastEditedPanel).active) {
        myBookLastEdited->setText(QString("Last edited: %1").arg(storyboardPanels().at(myLastEditedPanel).name));
    } else {
        myBookLastEdited->setText("No shots yet");
    }
    myBookGrid->setAlignment(Qt::AlignHCenter | Qt::AlignTop);

    for (int index = 0; index < storyboardPanels().size(); ++index) {
        const StoryboardPanel& panel = storyboardPanels().at(index);
        QFrame* card = new QFrame(myBookPage);
        card->setObjectName(panel.active ? "storyboardCard" : "emptyStoryboardCard");
        card->setFixedWidth(330);
        QVBoxLayout* cardLayout = new QVBoxLayout(card);
        cardLayout->setContentsMargins(12, 10, 12, 12);
        QHBoxLayout* cardHeader = new QHBoxLayout();
        QLabel* label = new QLabel(panel.active ? panel.name : QString("Shot %1").arg(index + 1), card);
        label->setObjectName("cardTitle");
        cardHeader->addWidget(label);
        cardHeader->addStretch();
        if (panel.active) {
            const QString projectRoot = painterSpaceProjectRoot();
            const QString shotRelDir = QString("scenes/scene_%1/shot_%2")
                .arg(myCurrentScene + 1, 2, 10, QChar('0'))
                .arg(index + 1, 3, 10, QChar('0'));
            const QDir shotDir(QDir(projectRoot).filePath(shotRelDir));
            const QString videoPath = shotDir.filePath("video.mp4");
            const QString framesDir = shotDir.filePath("frames");
            const bool hasClip = QFileInfo::exists(videoPath) ||
                (QDir(framesDir).exists() && !QDir(framesDir).entryList(QStringList() << "frame.*.png", QDir::Files).isEmpty());
            if (hasClip) {
                QLabel* clipBadge = new QLabel("Clip Ready", card);
                clipBadge->setStyleSheet("background: #1e3328; color: #7ed9a6; border: 1px solid #35634b; border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700;");
                cardHeader->insertWidget(cardHeader->count() - 2, clipBadge);
            }

            QPushButton* remove = new QPushButton("−", card);
            remove->setObjectName("panelDeleteButton");
            remove->setFixedSize(28, 28);
            remove->setToolTip("Delete shot (files can be recovered from .deleted_shots)");
            remove->setCursor(Qt::PointingHandCursor);
            connect(remove, &QPushButton::clicked, this, [this, index]() { deleteStoryboardPanel(index); });
            cardHeader->addWidget(remove);
        }
        cardLayout->addLayout(cardHeader);

        QPushButton* open = new QPushButton(card);
        open->setObjectName(panel.active ? "storyboardPanelButton" : "emptyPanelButton");
        open->setMinimumHeight(150);
        open->setCursor(Qt::PointingHandCursor);
        if (panel.active && !panel.render.isNull()) {
            const QPixmap preview = QPixmap::fromImage(panel.render.scaled(310, 150, Qt::KeepAspectRatio,
                                                                            Qt::SmoothTransformation));
            open->setIcon(QIcon(preview));
            open->setIconSize(preview.size());
            open->setText("Open 3D shot");
            connect(open, &QPushButton::clicked, this, [this, index]() { openStoryboardPanel(index); });
        } else if (panel.active) {
            open->setText("＋\nContinue shot");
            connect(open, &QPushButton::clicked, this, [this, index]() { openStoryboardPanel(index); });
        } else {
            open->setText("＋\nStart shot");
            connect(open, &QPushButton::clicked, this, [this, index]() { startStoryboardPanelAt(index); });
        }
        cardLayout->addWidget(open, 1);

        if (panel.active) {
            const QString projectRoot = painterSpaceProjectRoot();
            const QString shotRelDir = QString("scenes/scene_%1/shot_%2")
                .arg(myCurrentScene + 1, 2, 10, QChar('0'))
                .arg(index + 1, 3, 10, QChar('0'));
            const QDir shotDir(QDir(projectRoot).filePath(shotRelDir));
            const QString videoPath = shotDir.filePath("video.mp4");
            const QString framesDir = shotDir.filePath("frames");
            const bool hasClip = QFileInfo::exists(videoPath) ||
                (QDir(framesDir).exists() && !QDir(framesDir).entryList(QStringList() << "frame.*.png", QDir::Files).isEmpty());

            if (hasClip) {
                QPushButton* play = new QPushButton("▶  Play Clip", card);
                play->setObjectName("primaryButton");
                play->setCursor(Qt::PointingHandCursor);
                play->setFixedHeight(32);
                const QString shotDirPath = shotDir.absolutePath();
                connect(play, &QPushButton::clicked, this, [this, shotDirPath]() {
                    playRenderedClipForShot(shotDirPath);
                });
                cardLayout->addWidget(play);
            }
        }
        myBookGrid->addWidget(card, index / 3, index % 3);
    }
}

void MainWindow::rebuildStoryboardScenes() {
    if (!myScenesGrid) return;
    while (QLayoutItem* item = myScenesGrid->takeAt(0)) {
        if (QWidget* widget = item->widget()) widget->deleteLater();
        delete item;
    }
    myScenesGrid->setAlignment(Qt::AlignHCenter | Qt::AlignTop);

    for (int index = 0; index < myStoryboardScenes.size(); ++index) {
        const StoryboardScene& scene = myStoryboardScenes.at(index);
        int activePanels = 0;
        for (const StoryboardPanel& panel : scene.panels) activePanels += panel.active ? 1 : 0;

        QFrame* card = new QFrame(myScenesPage);
        card->setObjectName(scene.configured ? "storyboardCard" : "emptyStoryboardCard");
        card->setFixedWidth(330);
        QVBoxLayout* cardLayout = new QVBoxLayout(card);
        cardLayout->setContentsMargins(12, 10, 12, 12);

        QHBoxLayout* headerRow = new QHBoxLayout();
        QLabel* label = new QLabel(scene.name, card);
        label->setObjectName("cardTitle");
        headerRow->addWidget(label);
        headerRow->addStretch();
        QLabel* statusBadge = new QLabel(scene.configured ? "Scene Ready" : "Step 1: Describe Scene", card);
        statusBadge->setStyleSheet(scene.configured
            ? "background: #223b2e; color: #7ed9a6; border: 1px solid #35634b; border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700;"
            : "background: #273849; color: #8ad0f5; border: 1px solid #416382; border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 700;");
        headerRow->addWidget(statusBadge);
        cardLayout->addLayout(headerRow);

        QString detailText;
        if (scene.configured) {
            if (!scene.prompt.isEmpty()) {
                const QString snippet = scene.prompt.length() > 48 ? scene.prompt.left(45) + "…" : scene.prompt;
                detailText = QString("\"%1\"  •  %2 of %3 shots").arg(snippet).arg(activePanels).arg(scene.panels.size());
            } else {
                detailText = QString("3D editor scene  •  %1 of %2 shots in use").arg(activePanels).arg(scene.panels.size());
            }
        } else {
            detailText = QString("Click to describe scene (LLM prompt or 3D editor)");
        }
        QLabel* detail = new QLabel(detailText, card);
        detail->setObjectName("mutedText");
        detail->setWordWrap(true);
        cardLayout->addWidget(detail);

        QPushButton* open = new QPushButton(card);
        open->setObjectName(scene.configured ? "storyboardPanelButton" : "emptyPanelButton");
        open->setMinimumHeight(128);
        open->setCursor(Qt::PointingHandCursor);
        if (scene.configured && !scene.render.isNull()) {
            const QPixmap preview = QPixmap::fromImage(scene.render.scaled(310, 128, Qt::KeepAspectRatio,
                                                                           Qt::SmoothTransformation));
            open->setIcon(QIcon(preview));
            open->setIconSize(preview.size());
            open->setText("View Shots (Panels)  →");
        } else if (scene.configured) {
            open->setText("View Shots (Panels)  →");
        } else {
            open->setText("＋\nDescribe scene  →");
        }
        connect(open, &QPushButton::clicked, this, [this, index]() { openStoryboardScene(index); });
        cardLayout->addWidget(open, 1);

        if (scene.configured) {
            QHBoxLayout* cardActionRow = new QHBoxLayout();
            cardActionRow->setContentsMargins(0, 4, 0, 0);
            QPushButton* editMainSceneBtn = new QPushButton("✎  Edit Main Scene", card);
            editMainSceneBtn->setObjectName("quietButton");
            editMainSceneBtn->setCursor(Qt::PointingHandCursor);
            editMainSceneBtn->setToolTip("Open and edit the shared main scene (scene.usda) in 3D");
            connect(editMainSceneBtn, &QPushButton::clicked, this, [this, index]() { openSceneSetup(index); });
            cardActionRow->addWidget(editMainSceneBtn);
            cardActionRow->addStretch();
            cardLayout->addLayout(cardActionRow);
        }

        myScenesGrid->addWidget(card, index / 3, index % 3);
    }
}

QPushButton* MainWindow::makeAssetButton(const QString& text, const QString& objectName) {
    QPushButton* button = new QPushButton(text, this);
    button->setObjectName(objectName);
    button->setStyleSheet(toolStyle() + "QPushButton { padding: 7px; text-align: left; }");
    button->setCursor(Qt::PointingHandCursor);
    return button;
}

static QString folderToCategoryName(const QString& folderName) {
    QString name = folderName;
    name.replace('_', ' ');
    name.replace('-', ' ');
    const QStringList parts = name.split(' ', Qt::SkipEmptyParts);
    QStringList titleParts;
    for (const QString& p : parts) {
        if (!p.isEmpty()) {
            QString cap = p;
            cap[0] = cap[0].toUpper();
            titleParts.append(cap);
        }
    }
    return titleParts.join(' ');
}

void MainWindow::refreshAssetList() {
    if (!myAssetListLayout) return;
    const int savedIndex = myAssetCategoryCombo ? myAssetCategoryCombo->currentIndex() : 0;
    const QString savedCat = myAssetCategoryCombo ? myAssetCategoryCombo->itemData(savedIndex).toString() : QString();

    QLayoutItem* child;
    while ((child = myAssetListLayout->takeAt(0)) != nullptr) {
        if (child->widget()) {
            delete child->widget();
        }
        delete child;
    }

    populateAssetEntries();

    if (myAssetCategoryCombo && !savedCat.isEmpty()) {
        for (int i = 0; i < myAssetCategoryCombo->count(); ++i) {
            if (myAssetCategoryCombo->itemData(i).toString() == savedCat) {
                myAssetCategoryCombo->setCurrentIndex(i);
                break;
            }
        }
    }
}

void MainWindow::populateAssetEntries() {
    if (!myAssetListLayout) return;
    myAssetEntries.clear();
    const QString projectRoot = painterSpaceProjectRoot();

    // Automatically synchronize manifest.json with assets/usdc_assets/
    syncManifestForProject(projectRoot);

    QString manifestPath = QDir(projectRoot).filePath("manifest.json");
    if (!QFileInfo::exists(manifestPath)) {
        manifestPath = QDir(projectRoot).filePath("assets/manifest.json");
    }
    const QJsonObject manifestObj = readJsonFile(manifestPath);
    QMap<QString, QJsonObject> manifestByPath;
    for (const QJsonValue& val : manifestObj.value("assets").toArray()) {
        const QJsonObject obj = val.toObject();
        const QString p = obj.value("path").toString();
        if (!p.isEmpty()) {
            manifestByPath.insert(p, obj);
        }
        const QString sp = obj.value("source_path").toString();
        if (!sp.isEmpty()) {
            manifestByPath.insert(sp, obj);
        }
    }

    QTimer* assetRescanTimer = findChild<QTimer*>(QStringLiteral("assetRescanTimer"), Qt::FindDirectChildrenOnly);
    if (!assetRescanTimer) {
        assetRescanTimer = new QTimer(this);
        assetRescanTimer->setObjectName(QStringLiteral("assetRescanTimer"));
        assetRescanTimer->setSingleShot(true);
        assetRescanTimer->setInterval(350);
        connect(assetRescanTimer, &QTimer::timeout, this, &MainWindow::refreshAssetList);
    }
    QFileSystemWatcher* assetDirWatcher = findChild<QFileSystemWatcher*>(QStringLiteral("assetDirWatcher"), Qt::FindDirectChildrenOnly);
    if (!assetDirWatcher) {
        assetDirWatcher = new QFileSystemWatcher(this);
        assetDirWatcher->setObjectName(QStringLiteral("assetDirWatcher"));
        connect(assetDirWatcher, &QFileSystemWatcher::directoryChanged, this, [assetRescanTimer](const QString&) {
            if (assetRescanTimer) assetRescanTimer->start();
        });
    }

    // 1. Dynamic folder-based categories under assets/usdc_assets
    const QDir usdcBaseDir(QDir(projectRoot).filePath("assets/usdc_assets"));
    if (usdcBaseDir.exists()) {
        if (!assetDirWatcher->directories().contains(usdcBaseDir.absolutePath())) {
            assetDirWatcher->addPath(usdcBaseDir.absolutePath());
        }
        const QStringList subdirs = usdcBaseDir.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
        for (const QString& subdir : subdirs) {
            const QDir catDir(usdcBaseDir.filePath(subdir));
            if (!assetDirWatcher->directories().contains(catDir.absolutePath())) {
                assetDirWatcher->addPath(catDir.absolutePath());
            }
            // Watch for new clips so manifest sync runs, but never list them as placeable assets.
            if (subdir == QStringLiteral("human_actions")) continue;
            const QStringList subCatDirs = catDir.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name);
            for (const QString& scd : subCatDirs) {
                const QString scPath = catDir.filePath(scd);
                if (!assetDirWatcher->directories().contains(scPath)) {
                    assetDirWatcher->addPath(scPath);
                }
            }

            const QString categoryName = folderToCategoryName(subdir);
            QSet<QString> seenStems;

            QDirIterator it(catDir.absolutePath(), QStringList() << QStringLiteral("*.usdc") << QStringLiteral("*.usda"), QDir::Files, QDirIterator::Subdirectories);
            while (it.hasNext()) {
                it.next();
                const QString absFilePath = it.filePath();
                const QString relPath = QDir(projectRoot).relativeFilePath(absFilePath);
                const QString file = it.fileName();
                const QString ext = file.endsWith(".usdc", Qt::CaseInsensitive) ? QStringLiteral(".usdc") : QStringLiteral(".usda");
                QString stem = file;
                stem.chop(ext.length());
                // The Humans category offers only the three character models.
                if (subdir == QStringLiteral("humans") &&
                    stem != QStringLiteral("male") &&
                    stem != QStringLiteral("female") &&
                    stem != QStringLiteral("human")) continue;

                // Skip standalone wind-simulation demo scenes, normalization wrappers, and backups
                if (ext == ".usda" && (stem.endsWith(".norm", Qt::CaseInsensitive) || stem.contains("_demo", Qt::CaseInsensitive) || stem.endsWith("demo", Qt::CaseInsensitive))) {
                    continue;
                }
                if (stem.contains("backup", Qt::CaseInsensitive) || file.endsWith(".bak", Qt::CaseInsensitive)) {
                    continue;
                }
                if (file.compare(QStringLiteral("detailed_car.usdc"), Qt::CaseInsensitive) == 0) {
                    continue;
                }

                if (ext == ".usda" && seenStems.contains(stem)) {
                    continue; // prefer compiled .usdc if both exist
                }
                seenStems.insert(stem);

                // Format display name
                QString displayName = stem;
                if (stem == QStringLiteral("male")) {
                    displayName = QStringLiteral("Male (Bryan)");
                } else if (stem == QStringLiteral("female")) {
                    displayName = QStringLiteral("Female (Sofie)");
                } else if (stem == QStringLiteral("human")) {
                    displayName = QStringLiteral("General Human Figure");
                } else {
                    displayName.replace('_', ' ');
                    displayName.replace('-', ' ');
                    const QStringList words = displayName.split(' ', Qt::SkipEmptyParts);
                    QStringList capWords;
                    for (const QString& w : words) {
                        if (!w.isEmpty()) {
                            QString c = w;
                            c[0] = c[0].toUpper();
                            capWords.append(c);
                        }
                    }
                    displayName = capWords.join(' ');
                }

                // Clean prim base name
                QString primBase = displayName;
                primBase.replace(QRegularExpression("[^A-Za-z0-9_]"), "");
                if (primBase.isEmpty()) primBase = "Asset";

                AssetEntry entry;
                entry.name = displayName;
                entry.category = categoryName;
                entry.id = QString("%1.%2").arg(subdir, stem);
                entry.path = relPath;
                entry.basePrimName = primBase;
                entry.forwardAxis = (subdir == "humans" || subdir == "cars") ? QStringLiteral("+Z") : QStringLiteral("-Z");
                if (manifestByPath.contains(entry.path)) {
                    const QJsonObject mEntry = manifestByPath.value(entry.path);
                    const QString mId = mEntry.value("id").toString();
                    if (!mId.isEmpty()) {
                        entry.id = mId;
                    }
                    const QString mPath = mEntry.value("path").toString();
                    if (!mPath.isEmpty()) {
                        entry.path = mPath;
                    }
                    const QString mFwd = mEntry.value("forward_axis").toString();
                    if (!mFwd.isEmpty() && mFwd != QStringLiteral("unknown")) {
                        entry.forwardAxis = mFwd;
                    }
                }
                entry.defaultScale = (stem.contains("tower", Qt::CaseInsensitive)) ? 0.25 : 1.0;
                myAssetEntries.append(entry);
            }
        }
    }

    // 2. Procedural Props
    const struct PropDef {
        const char* name;
        AssetType type;
    } props[] = {
        {"Tree", AssetType::Tree},
        {"Mountain ridge", AssetType::Mountain},
        {"Rock group", AssetType::Rock},
        {"Light source", AssetType::Light},
        {"3D text block", AssetType::TextBlock}
    };
    for (const auto& p : props) {
        AssetEntry entry;
        entry.name = p.name;
        entry.category = QStringLiteral("Procedural Props");
        entry.id = QString("prop.%1").arg(p.name);
        entry.isProcedural = true;
        entry.proceduralType = p.type;
        myAssetEntries.append(entry);
    }

    // 3. Facial Expressions (if directory exists)
    const QDir expressionsDir(QDir(projectRoot).filePath("assets/facial_expressions"));
    if (expressionsDir.exists()) {
        const QStringList exprFiles = expressionsDir.entryList(QStringList() << "*.obj", QDir::Files, QDir::Name);
        for (const QString& file : exprFiles) {
            QString stem = file;
            stem.chop(4); // remove .obj
            QString title = stem;
            if (!title.isEmpty()) title[0] = title[0].toUpper();

            AssetEntry entry;
            entry.name = QString("%1 (Expression)").arg(title);
            entry.category = QStringLiteral("Facial Expressions");
            entry.id = "expression." + stem;
            entry.path = "assets/facial_expressions/" + file;
            entry.isFacialExpression = true;
            myAssetEntries.append(entry);
        }
    }

    // Collect unique categories in order of appearance
    QStringList orderedCategories;
    QMap<QString, int> categoryCounts;
    for (const AssetEntry& entry : myAssetEntries) {
        if (!categoryCounts.contains(entry.category)) {
            orderedCategories.append(entry.category);
        }
        categoryCounts[entry.category]++;
    }

    // Create buttons in myAssetListLayout
    for (int i = 0; i < myAssetEntries.size(); ++i) {
        AssetEntry& entry = myAssetEntries[i];
        QPushButton* btn = makeAssetButton(QString("＋  %1").arg(entry.name), "assetBtn_" + entry.id);
        btn->setToolTip(QString("Category: %1\nPath: %2\nClick to add to 3D stage and prompt").arg(entry.category, entry.path));
        connect(btn, &QPushButton::clicked, this, [this, i]() {
            if (i >= 0 && i < myAssetEntries.size()) {
                handleAssetClicked(myAssetEntries.at(i));
            }
        });
        entry.button = btn;
        myAssetListLayout->addWidget(btn);
    }
    myAssetListLayout->addStretch();

    // Populate category dropdown dynamically
    if (myAssetCategoryCombo) {
        QSignalBlocker blocker(myAssetCategoryCombo);
        myAssetCategoryCombo->clear();
        myAssetCategoryCombo->addItem(QString("All Categories (%1)").arg(myAssetEntries.size()), QString());
        for (const QString& cat : orderedCategories) {
            myAssetCategoryCombo->addItem(QString("%1 (%2)").arg(cat).arg(categoryCounts.value(cat)), cat);
        }
    }
}

void MainWindow::filterAssetsByCategory(int index) {
    if (!myAssetCategoryCombo) return;
    const QString filterCat = myAssetCategoryCombo->itemData(index).toString();

    for (const AssetEntry& entry : myAssetEntries) {
        if (entry.button) {
            entry.button->setVisible(filterCat.isEmpty() || entry.category == filterCat);
        }
    }
}

void MainWindow::handleAssetClicked(const AssetEntry& entry) {
    if (entry.isProcedural) {
        if (myUsdViewport) {
            myUsdViewport->addProceduralAsset(entry.proceduralType, entry.name);
        }
        if (myCanvasStatus) {
            myCanvasStatus->setText(QString("Added procedural '%1' to 3D stage").arg(entry.name));
        }
        return;
    }

    if (entry.isFacialExpression) {
        useUsdAssetInPrompt(entry.path);
        if (myCanvasStatus) {
            myCanvasStatus->setText(QString("Added facial expression '%1' to prompt").arg(entry.name));
        }
        return;
    }

    // USDC Asset
    if (myUsdViewport) {
        myUsdViewport->addReferencedAsset(entry.id, entry.path, entry.basePrimName, entry.forwardAxis, entry.defaultScale);
    }
    useUsdAssetInPrompt(entry.path);
    if (myCanvasStatus) {
        myCanvasStatus->setText(QString("Added '%1' to 3D stage and prompt").arg(entry.name));
    }
}

void MainWindow::addHuman() {
    if (myUsdViewport) {
        myUsdViewport->addReferencedAsset("character.human", "assets/usdc_assets/humans/human.norm.usda",
                                          "Human", "-Z", 1.0);
    }
}
void MainWindow::addTree() {
    if (myUsdViewport) myUsdViewport->addProceduralAsset(AssetType::Tree, "Tree");
}
void MainWindow::addMountain() {
    if (myUsdViewport) myUsdViewport->addProceduralAsset(AssetType::Mountain, "Mountain");
}
void MainWindow::addRock() {
    if (myUsdViewport) myUsdViewport->addProceduralAsset(AssetType::Rock, "Rock");
}
void MainWindow::addLight() {
    if (myUsdViewport) myUsdViewport->addProceduralAsset(AssetType::Light, "Light");
}
void MainWindow::addTextBlock() {
    if (myUsdViewport) myUsdViewport->addProceduralAsset(AssetType::TextBlock, "TextBlock");
}
void MainWindow::addDowntownTower() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/environments/downtown_art_deco_tower.usdc")
            : QStringLiteral("../../../assets/usdc_assets/environments/downtown_art_deco_tower.usdc");
        myUsdViewport->addReferencedAsset("downtown_art_deco_tower", rel, "DowntownTower", "-Z", 0.25);
    }
}
void MainWindow::addResidentialNeighborhood() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/environments/residential_neighborhood.usdc")
            : QStringLiteral("../../../assets/usdc_assets/environments/residential_neighborhood.usdc");
        myUsdViewport->addReferencedAsset("residential_neighborhood", rel, "ResidentialNeighborhood", "-Z", 1.0);
    }
}
void MainWindow::addResidentialIntersection() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/environments/residential_intersection.usdc")
            : QStringLiteral("../../../assets/usdc_assets/environments/residential_intersection.usdc");
        myUsdViewport->addReferencedAsset("residential_intersection", rel, "ResidentialIntersection", "-Z", 1.0);
    }
}
void MainWindow::addResidentialAvenue() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/environments/residential_avenue.usdc")
            : QStringLiteral("../../../assets/usdc_assets/environments/residential_avenue.usdc");
        myUsdViewport->addReferencedAsset("residential_avenue", rel, "ResidentialAvenue", "-Z", 1.0);
    }
}
void MainWindow::addOcean() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/environments/ocean_horizon.usdc")
            : QStringLiteral("../../../assets/usdc_assets/environments/ocean_horizon.usdc");
        myUsdViewport->addReferencedAsset("ocean_horizon", rel, "Ocean", "-Z", 1.0);
    }
}
void MainWindow::addGrandHarbour() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/environments/grand_harbour.usdc")
            : QStringLiteral("../../../assets/usdc_assets/environments/grand_harbour.usdc");
        myUsdViewport->addReferencedAsset("grand_harbour", rel, "GrandHarbour", "-Z", 1.0);
    }
}
void MainWindow::addMustang() {
    if (myUsdViewport) {
        const QString rel = (myCurrentPanel < 0)
            ? QStringLiteral("../../assets/usdc_assets/vehicles/mustang_fastback.usdc")
            : QStringLiteral("../../../assets/usdc_assets/vehicles/mustang_fastback.usdc");
        myUsdViewport->addReferencedAsset("mustang_fastback", rel, "Car", "+Z", 1.0);
    }
}
void MainWindow::addPontiac() {
    if (myUsdViewport) {
        myUsdViewport->addProceduralAsset(AssetType::Pontiac, "Pontiac");
    }
}

void MainWindow::syncLayers() {
    QSignalBlocker blocker(myLayers);
    myLayers->clear();
    if (!myUsdViewport) return;
    const QVector<UsdPrimItem> entries = myUsdViewport->primList();
    const QString selectedPath = myUsdViewport->selectedPrimPath();
    for (int i = 0; i < entries.size(); ++i) {
        const UsdPrimItem& e = entries.at(i);
        QString label;
        if (!e.actorId.isEmpty() || !e.actorLabel.isEmpty()) {
            const QString actorName = !e.actorLabel.isEmpty() ? e.actorLabel : e.actorId;
            label = QStringLiteral("👤 %1 [%2]").arg(actorName, e.actorId.isEmpty() ? e.name : e.actorId);
        } else {
            const QString indent(qMax(0, (e.depth - 1) * 2), QChar(' '));
            label = indent + e.name + QString(" (%1)").arg(e.typeName);
        }
        if (e.hasUserEdit) {
            label += QStringLiteral("  • [edited]");
        }
        QListWidgetItem* item = new QListWidgetItem(label, myLayers);
        item->setData(Qt::UserRole, e.path);
        item->setToolTip(e.path + (e.assetId.isEmpty() ? QString() : QString("  [asset_id=%1]").arg(e.assetId)));
        item->setCheckState(e.visible ? Qt::Checked : Qt::Unchecked);
        if (!e.actorId.isEmpty()) {
            item->setForeground(e.identityColor);
        } else if (e.hasUserEdit) {
            item->setForeground(QColor("#8ad0f5"));
        }
        if (e.path == selectedPath) {
            myLayers->setCurrentRow(i);
        }
    }
    if (myActionTimeline) {
        myActionTimeline->updateActorList(entries);
    }
}

void MainWindow::selectionChanged() {
    updateSelectionPanel();
    syncLayers();
}

void MainWindow::updateSelectionPanel() {
    if (!myUsdViewport) return;
    const QString primPath = myUsdViewport->selectedPrimPath();
    const bool hasSelection = !primPath.isEmpty();
    if (primPath == QStringLiteral("/World/ShotCam") || primPath.startsWith(QStringLiteral("/World/CameraRig"))) {
        mySelectionName->setText(QStringLiteral("🎥 /World/ShotCam (Director Camera)"));
    } else {
        mySelectionName->setText(hasSelection ? QString("📦 %1").arg(primPath) : "No asset selected");
    }
    mySelectionName->setToolTip(hasSelection ? primPath : QString());
    myPositionX->setEnabled(hasSelection);
    myPositionY->setEnabled(hasSelection);
    myPositionZ->setEnabled(hasSelection);
    myRotationX->setEnabled(hasSelection);
    myRotationY->setEnabled(hasSelection);
    myRotationZ->setEnabled(hasSelection);
    myScaleSpin->setEnabled(hasSelection);
    if (myRemoveAssetButton) {
        myRemoveAssetButton->setEnabled(hasSelection);
    }
    if (hasSelection) {
        const QVector3D position = myUsdViewport->selectedPrimPosition();
        const QVector3D rotation = myUsdViewport->selectedPrimRotation();
        const double scale = myUsdViewport->selectedPrimScale();
        QSignalBlocker blockX(myPositionX);
        QSignalBlocker blockY(myPositionY);
        QSignalBlocker blockZ(myPositionZ);
        QSignalBlocker blockRotationX(myRotationX);
        QSignalBlocker blockRotationY(myRotationY);
        QSignalBlocker blockRotationZ(myRotationZ);
        QSignalBlocker blockScale(myScaleSpin);
        myPositionX->setValue(position.x());
        myPositionY->setValue(position.y());
        myPositionZ->setValue(position.z());
        myRotationX->setValue(rotation.x());
        myRotationY->setValue(rotation.y());
        myRotationZ->setValue(rotation.z());
        myScaleSpin->setValue(scale);
    } else {
        QSignalBlocker blockX(myPositionX);
        QSignalBlocker blockY(myPositionY);
        QSignalBlocker blockZ(myPositionZ);
        QSignalBlocker blockRotationX(myRotationX);
        QSignalBlocker blockRotationY(myRotationY);
        QSignalBlocker blockRotationZ(myRotationZ);
        QSignalBlocker blockScale(myScaleSpin);
        myPositionX->setValue(0.0);
        myPositionY->setValue(0.0);
        myPositionZ->setValue(0.0);
        myRotationX->setValue(0.0);
        myRotationY->setValue(0.0);
        myRotationZ->setValue(0.0);
        myScaleSpin->setValue(1.0);
    }
    updatePrimPathControls();
    updateCameraInspectorUi();
}

void MainWindow::toggleDrawPathForSelectedPrim() {
    if (!myUsdViewport || !myPrimDrawPathButton) return;
    const QString sel = myUsdViewport->selectedPrimPath();
    if (sel.isEmpty()) return;

    if (myPrimDrawPathButton->isChecked()) {
        myUsdViewport->startDrawingPath(sel);
    } else {
        myUsdViewport->stopDrawingPath();
    }
    updatePrimPathControls();
}

void MainWindow::clearPathForSelectedPrim() {
    if (!myUsdViewport) return;
    const QString sel = myUsdViewport->selectedPrimPath();
    if (sel.isEmpty()) return;
    myUsdViewport->clearPrimPath(sel);
    updatePrimPathControls();
}

void MainWindow::updatePrimPathControls() {
    if (!myUsdViewport || !myPrimDrawPathButton) return;
    const QString sel = myUsdViewport->selectedPrimPath();
    const bool hasSel = !sel.isEmpty();

    myPrimDrawPathButton->setEnabled(hasSel);
    if (!hasSel) {
        QSignalBlocker b(myPrimDrawPathButton);
        myPrimDrawPathButton->setChecked(false);
        myPrimDrawPathButton->setText("✏️ Draw Path");
        if (myPrimClearPathButton) myPrimClearPathButton->setEnabled(false);
        if (myPrimPathInfoLabel) myPrimPathInfoLabel->setText("No selection");
        return;
    }

    const bool isDrawingThis = (myUsdViewport->isDrawPathActive() && myUsdViewport->drawingPrimPath() == sel);
    {
        QSignalBlocker b(myPrimDrawPathButton);
        myPrimDrawPathButton->setChecked(isDrawingThis);
        myPrimDrawPathButton->setText(isDrawingThis ? "✏️ Drawing Path… (Drag in 3D)" : "✏️ Draw Path");
    }

    const bool hasPath = myUsdViewport->hasPrimPath(sel);
    const int numPts = myUsdViewport->primPathJointCount(sel);
    const QColor pCol = myUsdViewport->primPathColor(sel);

    if (myPrimClearPathButton) {
        myPrimClearPathButton->setEnabled(hasPath);
    }
    if (myPrimPathInfoLabel) {
        if (hasPath) {
            myPrimPathInfoLabel->setText(QString("<span style='color:%1; font-size:12px;'>●</span> %2 pts")
                                             .arg(pCol.name())
                                             .arg(numPts));
        } else {
            myPrimPathInfoLabel->setText(QString("<span style='color:%1; font-size:12px;'>●</span> Sketched: 0")
                                             .arg(pCol.name()));
        }
    }
}

void MainWindow::syncTimelineMovementDistances(bool recordUndo) {
    if (mySyncingTimelineMovementDistances || !myActionTimeline || !myUsdViewport || myCurrentPanel < 0) return;
    QHash<QString, double> distances;
    const auto actions = myActionTimeline->actions();
    const auto prims = myUsdViewport->primList();
    QSet<QString> actorRouteUsed;
    for (const auto& action : actions) {
        const QString behavior = action.action.toLower();
        if (!QStringList{"walk", "walk_to", "run", "run_to", "jog", "jog_to",
                         "sprint", "sprint_to", "flee_from", "drive", "drive_to"}.contains(behavior)) continue;
        QString routeKey;
        if (!action.pathId.isEmpty()) {
            const QString segmentKey = "/World/Paths/" + action.pathId;
            if (myUsdViewport->hasPrimPath(segmentKey)) routeKey = segmentKey;
        }
        if (routeKey.isEmpty() && !actorRouteUsed.contains(action.actorPath) &&
            myUsdViewport->hasPrimPath(action.actorPath)) {
            routeKey = action.actorPath;
            actorRouteUsed.insert(action.actorPath);
        }
        if (!routeKey.isEmpty()) {
            const auto points = myUsdViewport->primPath(routeKey);
            double meters = 0.0;
            for (int i = 1; i < points.size(); ++i) meters += (points[i] - points[i-1]).length();
            if (meters > 0.05) distances.insert(action.id, meters);
            continue;
        }
        if (action.target.isEmpty()) continue;
        QString targetPath;
        for (const auto& prim : prims) {
            if (prim.path == action.target || prim.actorId == action.target || prim.name == action.target) {
                targetPath = prim.path;
                break;
            }
        }
        QVector3D from, to;
        if (!targetPath.isEmpty() && myUsdViewport->primWorldPosition(action.actorPath, from) &&
            myUsdViewport->primWorldPosition(targetPath, to)) {
            const double meters = (to-from).length();
            if (meters > 0.05) distances.insert(action.id, meters);
        }
    }
    mySyncingTimelineMovementDistances = true;
    myActionTimeline->setMovementDistances(distances, recordUndo);
    mySyncingTimelineMovementDistances = false;
}

void MainWindow::saveShotActions(int sceneIndex, int panelIndex) {
    if (sceneIndex < 0 || panelIndex < 0 || !myActionTimeline || !myUsdViewport) return;
    const QString projectRoot = painterSpaceProjectRoot();
    const QString shotFolder = QString("scenes/scene_%1/shot_%2")
                                   .arg(sceneIndex + 1, 2, 10, QChar('0'))
                                   .arg(panelIndex + 1, 3, 10, QChar('0'));
    const QDir shotDir(QDir(projectRoot).filePath(shotFolder));
    if (!shotDir.exists()) return;

    QJsonObject rootObj;
    rootObj["duration_s"] = myActionTimeline->shotDuration();
    QJsonArray authoredActions;
    for (const auto& action : myActionTimeline->actions())
        if (!action.generated) authoredActions.append(action.toJson());
    rootObj["actions"] = authoredActions;
    rootObj["drawn_paths"] = QJsonArray::fromStringList(myUsdViewport->explicitlyDrawnPrimPaths());

    QJsonObject pathsObj;
    const auto allPaths = myUsdViewport->allPrimPaths();
    for (auto it = allPaths.begin(); it != allPaths.end(); ++it) {
        QJsonArray ptsArr;
        for (const auto& pt : it.value()) {
            QJsonArray ptArr;
            ptArr.append(pt.x());
            ptArr.append(pt.y());
            ptArr.append(pt.z());
            ptsArr.append(ptArr);
        }
        pathsObj[it.key()] = ptsArr;
    }
    rootObj["paths"] = pathsObj;

    QJsonObject brief;
    QFile previousBrief(shotDir.filePath("director_brief.json"));
    if (previousBrief.open(QIODevice::ReadOnly)) brief = QJsonDocument::fromJson(previousBrief.readAll()).object();
    brief["version"] = 1;
    brief["duration_s"] = myActionTimeline->shotDuration();
    brief["drawn_paths"] = rootObj["drawn_paths"];
    QJsonObject tracks, briefPaths;
    QHash<QString, QString> actorIds;
    for (const auto& prim : myUsdViewport->primList()) {
        if (!prim.actorId.isEmpty()) actorIds[prim.path] = prim.actorId;
    }
    QHash<QString, QString> authoredPaths;
    for (auto it = allPaths.begin(); it != allPaths.end(); ++it) {
        if (it.key().contains("Cam") || it.key().contains("Camera")) continue;
        QString actor = actorIds.value(it.key(), it.key().section('/', -1));
        QString actorPath = it.key();
        const bool segmentPath = it.key().startsWith("/World/Paths/");
        if (segmentPath) {
            for (const auto& seg : myActionTimeline->actions()) if (!seg.generated && seg.pathId == it.key().section('/', -1)) {
                actorPath = seg.actorPath; actor = actorIds.value(actorPath, actorPath.section('/', -1)); break;
            }
        }
        QString pid = segmentPath ? it.key().section('/', -1) : "path_" + actor;
        const auto oldPaths = brief["paths"].toObject();
        for (auto old = oldPaths.begin(); old != oldPaths.end(); ++old) {
            if (!segmentPath && old.value().toObject()["owner"].toString() == actor) { pid = old.key(); break; }
        }
        if (!segmentPath) authoredPaths[actor] = pid;
        QJsonObject path = brief["paths"].toObject()[pid].toObject();
        path["points"] = pathsObj[it.key()];
        path["owner"] = actor;
        for (const auto& prim : myUsdViewport->primList()) if (prim.path == actorPath) {
            path["asset_id"] = prim.assetId;
            path["actor_type"] = prim.assetId.contains("car") || prim.assetId.contains("vehicle") ? "vehicle" : "human";
            break;
        }
        path["actor_path"] = actorPath;
        path["viewport_key"] = it.key();
        path["closed"] = false;
        if (!path.contains("speed_profile")) path["speed_profile"] = "constant";
        for (const auto& seg : myActionTimeline->actions()) if (!seg.generated &&
            (seg.pathId == pid || (!segmentPath && seg.actorPath == actorPath && seg.pathId.isEmpty()))) {
            path["speed_profile"] = seg.speedProfile; break;
        }
        briefPaths[pid] = path;
    }
    QSet<QString> attached;
    for (const auto& action : myActionTimeline->actions()) {
        if (action.generated) continue;
        if (action.actorPath.contains("Cam") || action.actorPath.contains("Camera")) continue;
        const QString actor = actorIds.value(action.actorPath, action.actorPath.section('/', -1));
        QJsonObject track = tracks[actor].toObject();
        track["actor_path"] = action.actorPath;
        track["label"] = action.actorLabel;
        for (const auto& prim : myUsdViewport->primList()) if (prim.path == action.actorPath) { track["asset_id"] = prim.assetId; break; }
        QJsonArray segments = track["segments"].toArray();
        QJsonObject seg;
        // Preserve explicit flexibility and discrete alternatives from saved briefs.
        for (const auto& old : brief["tracks"].toObject()[actor].toObject()["segments"].toArray()) {
            if (old.toObject()["id"].toString() == action.id) seg = old.toObject();
        }
        seg["id"] = action.id;
        seg["t"] = QJsonArray{action.startS, action.endS};
        seg["action"] = action.action;
        if (!action.sourceBeatId.isEmpty()) seg["source_beat_id"] = action.sourceBeatId;
        else seg.remove("source_beat_id");
        if (!action.groupId.isEmpty()) {
            seg["group_id"] = action.groupId;
            QJsonArray participants, participantPaths;
            for (const auto& path : action.participants) {
                participants.append(actorIds.value(path, path.section('/', -1)));
                participantPaths.append(path);
            }
            seg["participants"] = participants;
            seg["participant_paths"] = participantPaths;
            if (action.action == "perform_activity") seg["activity"] = action.activity.isEmpty() ? "cards" : action.activity;
            else seg.remove("activity");
            if (action.action == "attack") {
                const int self = action.participants.indexOf(action.actorPath);
                const QString other = action.participants[(self + 1) % action.participants.size()];
                seg["target"] = actorIds.value(other, other.section('/', -1));
            } else seg.remove("target");
        } else {
            seg.remove("group_id"); seg.remove("participants"); seg.remove("participant_paths"); seg.remove("activity");
        }
        seg["lock"] = action.lock;
        seg["flexible"] = action.flexible;
        seg["tolerance_s"] = action.toleranceS;
        if (action.distanceM >= 0.0) seg["distance_m"] = action.distanceM; else seg.remove("distance_m");
        seg["auto_timing"] = action.autoTiming;
        if (action.groupId.isEmpty()) {
            if (!action.target.isEmpty()) seg["target"] = action.target; else seg.remove("target");
        }
        if (!action.note.isEmpty()) seg["note"] = action.note; else seg.remove("note");
        if (!action.pathId.isEmpty()) seg["path"] = action.pathId;
        else if (!attached.contains(actor) && authoredPaths.contains(actor) &&
                 QStringList{"walk", "run", "drive", "walk_to", "run_to", "drive_to", "jog_to", "flee_from"}.contains(action.action)) {
            seg["path"] = authoredPaths[actor];
            attached.insert(actor);
        }
        segments.append(seg);
        track["segments"] = segments;
        tracks[actor] = track;
    }
    brief["tracks"] = tracks;
    brief["paths"] = briefPaths;
    myUsdViewport->setDirectorBrief(brief);
    QFile briefFile(shotDir.filePath("director_brief.json"));
    if (briefFile.open(QIODevice::WriteOnly | QIODevice::Text))
        briefFile.write(QJsonDocument(brief).toJson(QJsonDocument::Indented));

    QFile file(shotDir.filePath("actions.json"));
    if (file.open(QIODevice::WriteOnly | QIODevice::Text)) {
        file.write(QJsonDocument(rootObj).toJson(QJsonDocument::Indented));
    }
}

void MainWindow::refreshGeneratedShotTimeline(const QString& stagePath) {
    if (!myActionTimeline || !stagePath.endsWith("/shot.usda")) return;
    QVector<ActionSegment> actions;
    QSet<QString> pinnedBeats;
    for (const auto& action : myActionTimeline->actions()) if (!action.generated) {
        actions.append(action);
        if (!action.sourceBeatId.isEmpty()) pinnedBeats.insert(action.sourceBeatId);
    }
    const QJsonObject timeline = readJsonFile(QFileInfo(stagePath).dir().filePath("resolved_timeline.json"));
    QHash<QString, QString> actorPaths, actorLabels;
    if (myUsdViewport) for (const auto& prim : myUsdViewport->primList()) if (!prim.actorId.isEmpty()) {
        if (!actorPaths.contains(prim.actorId)) {
            actorPaths[prim.actorId] = prim.path;
            actorLabels[prim.actorId] = prim.actorLabel.isEmpty() ? prim.actorId : prim.actorLabel;
        }
    }
    for (const auto& value : timeline.value("beats").toArray()) {
        const QJsonObject beat = value.toObject();
        const QJsonObject params = beat.value("params").toObject();
        const QString beatId = beat.value("beat_id").toString();
        const QString actorId = beat.value("actor_id").toString();
        if (beatId.isEmpty() || actorId.isEmpty() || beat.value("is_idle_fill").toBool() ||
            params.contains("brief_segment") || pinnedBeats.contains(beatId)) continue;
        ActionSegment action;
        action.id = "ai_" + beatId;
        action.sourceBeatId = beatId;
        action.generated = true;
        action.actorPath = actorPaths.value(actorId, "/World/" + actorId);
        action.actorLabel = actorLabels.value(actorId, actorId);
        action.startS = beat.value("start_time").toDouble();
        action.endS = beat.value("end_time").toDouble();
        action.action = beat.value("function").toString();
        action.activity = params.value("activity").toString();
        action.target = params.value("target").toString();
        action.groupId = params.value("group_id").toString();
        for (const auto& participant : params.value("participants").toArray()) {
            const QString id = participant.toString();
            action.participants.append(actorPaths.value(id, "/World/" + id));
        }
        action.color = QColor("#22d3ee");
        if (action.endS > action.startS && !action.action.isEmpty()) actions.append(action);
    }
    myActionTimeline->setActions(actions);
    const double duration = timeline.value("total_duration_s").toDouble();
    if (duration > 0 && !actions.isEmpty()) {
        myActionTimeline->setShotDuration(duration);
        // A generated route can outlast the editor's initial camera-only
        // duration. Extend an undrawn camera range to the compiled shot length
        // so its viewfinder does not stop after the original short default.
        if (myUsdViewport) {
            auto doc = myUsdViewport->cameraDocument();
            auto cameras = doc["cameras"].toObject();
            const QString cid = doc["active_camera"].toString();
            auto camera = cameras[cid].toObject();
            auto path = camera["path"].toObject();
            const auto range = path["time_range"].toArray();
            if (!path["user_drawn"].toBool() && range.size() == 2 &&
                range[1].toDouble() < duration) {
                const double oldEnd = range[1].toDouble();
                path["time_range"] = QJsonArray{range[0], duration};
                camera["path"] = path;
                auto aim = camera["aim"].toObject();
                auto segments = aim["segments"].toArray();
                for (int i = 0; i < segments.size(); ++i) {
                    auto segment = segments[i].toObject();
                    auto times = segment["t"].toArray();
                    if (times.size() == 2 && std::abs(times[1].toDouble() - oldEnd) < 1e-4) {
                        segment["t"] = QJsonArray{times[0], duration};
                        segments[i] = segment;
                    }
                }
                aim["segments"] = segments;
                camera["aim"] = aim;
                cameras[cid] = camera;
                doc["cameras"] = cameras;
                myUsdViewport->setCameraDocument(doc);
                myUsdViewport->bakeCurrentCameraRig();
                myActionTimeline->setCameraRange(range[0].toDouble(), duration, cid);
            }
        }
        if (myCurrentScene >= 0 && myCurrentPanel >= 0) {
            saveShotActions(myCurrentScene, myCurrentPanel);
        }
    }
    myActionTimeline->selectAction(QString());
}

void MainWindow::loadShotActions(int sceneIndex, int panelIndex) {
    if (sceneIndex < 0 || panelIndex < 0 || !myActionTimeline || !myUsdViewport) return;
    const QString projectRoot = painterSpaceProjectRoot();
    const QString shotFolder = QString("scenes/scene_%1/shot_%2")
                                   .arg(sceneIndex + 1, 2, 10, QChar('0'))
                                   .arg(panelIndex + 1, 3, 10, QChar('0'));
    const QDir shotDir(QDir(projectRoot).filePath(shotFolder));
    const bool hasBrief = QFileInfo::exists(shotDir.filePath("director_brief.json"));
    const QString actionsFilePath = shotDir.filePath(hasBrief ? "director_brief.json" : "actions.json");

    if (!QFileInfo::exists(actionsFilePath)) {
        myActionTimeline->setActions({});
        myActionTimeline->setShotDuration(15.0);
        refreshGeneratedShotTimeline(shotDir.filePath("shot.usda"));
        myActionTimeline->clearUndoHistory();
        myGlobalUndoStack.clear();
        updateEditorUndoButtonState();
        return;
    }

    QFile file(actionsFilePath);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return;

    QJsonObject rootObj = QJsonDocument::fromJson(file.readAll()).object();
    if (hasBrief) {
        myUsdViewport->setDirectorBrief(rootObj);
        QJsonArray actions;
        double duration = rootObj["duration_s"].toDouble(15.0);
        const auto tracks = rootObj["tracks"].toObject();
        for (auto tr = tracks.begin(); tr != tracks.end(); ++tr) {
            const auto track = tr.value().toObject();
            for (const auto& value : track["segments"].toArray()) {
                auto seg = value.toObject();
                const auto t = seg["t"].toArray();
                seg["start_s"] = t.at(0); seg["end_s"] = t.at(1);
                seg["speed_profile"] = rootObj["paths"].toObject()[seg["path"].toString()].toObject()["speed_profile"].toString("constant");
                seg["actor_path"] = track["actor_path"].toString("/World/" + tr.key());
                seg["actor_label"] = track["label"].toString(tr.key());
                if (seg.contains("participant_paths")) seg["participants"] = seg["participant_paths"];
                duration = qMax(duration, t.at(1).toDouble());
                actions.append(seg);
            }
        }
        QJsonObject paths;
        const auto savedPaths = rootObj["paths"].toObject();
        for (auto it = savedPaths.begin(); it != savedPaths.end(); ++it) {
            auto path = it.value().toObject();
            paths[path["viewport_key"].toString(path["actor_path"].toString("/World/" + path["owner"].toString()))] = path["points"];
        }
        rootObj["actions"] = actions; rootObj["paths"] = paths;
        rootObj["duration_s"] = qMax(1.0, duration);
    }
    if (rootObj.contains("duration_s")) {
        myActionTimeline->setShotDuration(rootObj["duration_s"].toDouble(15.0));
    }
    if (rootObj.contains("actions")) {
        myActionTimeline->fromJson(rootObj["actions"].toArray());
    }
    myActionTimeline->clearUndoHistory();
    myGlobalUndoStack.clear();
    updateEditorUndoButtonState();

    if (rootObj.contains("paths")) {
        QHash<QString, QVector<QVector3D>> loadedPaths;
        const QJsonObject pathsObj = rootObj["paths"].toObject();
        for (auto it = pathsObj.begin(); it != pathsObj.end(); ++it) {
            QVector<QVector3D> pts;
            const QJsonArray ptsArr = it.value().toArray();
            for (const auto& val : ptsArr) {
                const QJsonArray ptArr = val.toArray();
                if (ptArr.size() >= 3) {
                    pts.append(QVector3D(ptArr.at(0).toDouble(), ptArr.at(1).toDouble(), ptArr.at(2).toDouble()));
                }
            }
            loadedPaths[it.key()] = pts;
        }
        myUsdViewport->setAllPrimPaths(loadedPaths);
    }
    if (rootObj.contains("drawn_paths")) {
        QStringList drawn;
        for (const auto& value : rootObj["drawn_paths"].toArray()) drawn.append(value.toString());
        myUsdViewport->setExplicitlyDrawnPrimPaths(drawn);
    }
    syncTimelineMovementDistances(false);
    refreshGeneratedShotTimeline(shotDir.filePath("shot.usda"));
}

void MainWindow::updateEditorUndoButtonState() {
    if (!myEditorUndoButton) return;
    const bool canUndoViewport = (myUsdViewport && myUsdViewport->canUndo());
    const bool canUndoTimeline = (myActionTimeline && myActionTimeline->canUndo());
    myEditorUndoButton->setEnabled(canUndoViewport || canUndoTimeline);
}

void MainWindow::performGlobalUndo() {
    while (!myGlobalUndoStack.isEmpty()) {
        const UndoActionSource src = myGlobalUndoStack.takeLast();
        if (src == UndoActionSource::Timeline) {
            if (myActionTimeline && myActionTimeline->canUndo()) {
                myActionTimeline->undo();
                if (myCanvasStatus) {
                    myCanvasStatus->setText("Undid action timeline edit");
                }
                updateEditorUndoButtonState();
                return;
            }
        } else if (src == UndoActionSource::Viewport) {
            if (myUsdViewport && myUsdViewport->canUndo()) {
                myUsdViewport->undo();
                updateSelectionPanel();
                syncLayers();
                updateCameraInspectorUi();
                if (myCanvasStatus) {
                    myCanvasStatus->setText("Undid viewport edit");
                }
                updateEditorUndoButtonState();
                return;
            }
        }
    }

    // Fallback if global history stack is empty:
    if (myActionTimeline && myActionTimeline->canUndo() && (myActionTimeline->hasFocus() || !myUsdViewport || !myUsdViewport->canUndo())) {
        myActionTimeline->undo();
        if (myCanvasStatus) {
            myCanvasStatus->setText("Undid action timeline edit");
        }
    } else if (myUsdViewport && myUsdViewport->canUndo()) {
        myUsdViewport->undo();
        updateSelectionPanel();
        syncLayers();
        updateCameraInspectorUi();
        if (myCanvasStatus) {
            myCanvasStatus->setText("Undid viewport edit");
        }
    } else if (myActionTimeline && myActionTimeline->canUndo()) {
        myActionTimeline->undo();
        if (myCanvasStatus) {
            myCanvasStatus->setText("Undid action timeline edit");
        }
    }
    updateEditorUndoButtonState();
}

void MainWindow::setShotMode(ShotMode mode) {
    myShotMode = mode;
    if (myShotModeCombo) {
        QSignalBlocker blocker(myShotModeCombo);
        for (int i = 0; i < myShotModeCombo->count(); ++i) {
            if (myShotModeCombo->itemData(i).toInt() == static_cast<int>(mode)) {
                myShotModeCombo->setCurrentIndex(i);
                break;
            }
        }
    }
    if (myCanvasStatus) {
        switch (mode) {
            case ShotMode::DirectorManual:
                myCanvasStatus->setText("Director Mode: Manual Camera Active & Locked (LLM will not override)");
                break;
            case ShotMode::PromptLlm:
                myCanvasStatus->setText("Shot Mode: Prompt / LLM (LLM synthesizes camera)");
                break;
            case ShotMode::Hybrid:
                myCanvasStatus->setText("Hybrid Mode: LLM Camera converted to editable Director Rig");
                break;
        }
    }
}

void MainWindow::setupCameraInspector(QVBoxLayout* parentLayout) {
    if (!parentLayout) return;

    myCameraInspectorBox = new QGroupBox("CAMERA DIRECTOR", this);
    myCameraInspectorBox->setStyleSheet(directorPanelStyle());
    QVBoxLayout* camLayout = new QVBoxLayout(myCameraInspectorBox);
    camLayout->setContentsMargins(8, 12, 8, 8);
    camLayout->setSpacing(6);

    // Viewfinder toggle button with real-time state
    myCameraViewModeButton = new QPushButton(myCameraInspectorBox);
    myCameraViewModeButton->setCursor(Qt::PointingHandCursor);
    myCameraViewModeButton->setToolTip("Toggle between Camera Viewfinder and 3D World Editor (Hotkey: Tab)");
    connect(myCameraViewModeButton, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->toggleViewMode();
            updateCameraInspectorUi();
        }
    });
    camLayout->addWidget(myCameraViewModeButton);

    // Mode row
    QHBoxLayout* modeRow = new QHBoxLayout();
    modeRow->setSpacing(6);
    QLabel* modeLbl = new QLabel("Mode", myCameraInspectorBox);
    modeLbl->setFixedWidth(38);
    modeLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    modeRow->addWidget(modeLbl);

    myShotModeCombo = new QComboBox(myCameraInspectorBox);
    myShotModeCombo->addItem("Director Manual (Locked)", static_cast<int>(ShotMode::DirectorManual));
    myShotModeCombo->addItem("Prompt / LLM (Auto)", static_cast<int>(ShotMode::PromptLlm));
    myShotModeCombo->addItem("Hybrid (LLM → Rig)", static_cast<int>(ShotMode::Hybrid));
    myShotModeCombo->setCurrentIndex(0);
    connect(myShotModeCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int idx) {
        if (idx >= 0) {
            setShotMode(static_cast<ShotMode>(myShotModeCombo->currentData().toInt()));
        }
    });
    modeRow->addWidget(myShotModeCombo, 1);
    camLayout->addLayout(modeRow);

    // Shot Size row
    QHBoxLayout* sizeRow = new QHBoxLayout();
    sizeRow->setSpacing(6);
    QLabel* sizeLbl = new QLabel("Size", myCameraInspectorBox);
    sizeLbl->setFixedWidth(38);
    sizeLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    sizeRow->addWidget(sizeLbl);

    myCameraShotSizeCombo = new QComboBox(myCameraInspectorBox);
    myCameraShotSizeCombo->addItem("Extreme Wide", static_cast<int>(CameraShotSize::ExtremeWide));
    myCameraShotSizeCombo->addItem("Wide (Full)", static_cast<int>(CameraShotSize::Wide));
    myCameraShotSizeCombo->addItem("Medium (Waist)", static_cast<int>(CameraShotSize::Medium));
    myCameraShotSizeCombo->addItem("Medium Close-Up", static_cast<int>(CameraShotSize::MediumCloseUp));
    myCameraShotSizeCombo->addItem("Close-Up (Face)", static_cast<int>(CameraShotSize::CloseUp));
    myCameraShotSizeCombo->addItem("Extreme Close-Up", static_cast<int>(CameraShotSize::ExtremeCloseUp));
    myCameraShotSizeCombo->addItem("Over-The-Shoulder", static_cast<int>(CameraShotSize::OverTheShoulder));
    myCameraShotSizeCombo->addItem("Top-Down", static_cast<int>(CameraShotSize::TopDown));
    myCameraShotSizeCombo->setCurrentIndex(2);
    connect(myCameraShotSizeCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int idx) {
        if (idx >= 0 && myUsdViewport) {
            CameraShotSize sz = static_cast<CameraShotSize>(myCameraShotSizeCombo->currentData().toInt());
            const bool moveCam = myCameraFramingModeCheck ? myCameraFramingModeCheck->isChecked() : true;
            myUsdViewport->applyShotSizePreset(sz, moveCam);
            updateCameraInspectorUi();
        }
    });
    sizeRow->addWidget(myCameraShotSizeCombo, 1);
    camLayout->addLayout(sizeRow);

    // Focal length header + slider
    QHBoxLayout* zoomHeader = new QHBoxLayout();
    zoomHeader->setContentsMargins(0, 2, 0, 0);
    QLabel* focalLbl = new QLabel("Focal Length", myCameraInspectorBox);
    focalLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    zoomHeader->addWidget(focalLbl);
    zoomHeader->addStretch();
    myCameraZoomValueLabel = new QLabel("35 mm", myCameraInspectorBox);
    myCameraZoomValueLabel->setStyleSheet("font-size: 11px; font-weight: 700; color: #58a6ff;");
    zoomHeader->addWidget(myCameraZoomValueLabel);
    camLayout->addLayout(zoomHeader);

    myCameraZoomSlider = new QSlider(Qt::Horizontal, myCameraInspectorBox);
    myCameraZoomSlider->setRange(12, 200);
    myCameraZoomSlider->setValue(35);
    myCameraZoomSlider->setStyleSheet(
        "QSlider::groove:horizontal { height: 4px; background: #28303a; border-radius: 2px; } "
        "QSlider::sub-page:horizontal { background: #388bfd; border-radius: 2px; } "
        "QSlider::handle:horizontal { background: #e6edf3; width: 14px; margin-top: -5px; margin-bottom: -5px; border-radius: 7px; } "
        "QSlider::handle:horizontal:hover { background: #ffffff; }");
    connect(myCameraZoomSlider, &QSlider::valueChanged, this, [this](int val) {
        if (myCameraZoomValueLabel) {
            myCameraZoomValueLabel->setText(QString("%1 mm").arg(val));
        }
        if (myUsdViewport) {
            myUsdViewport->setCameraFocalLength(static_cast<double>(val));
        }
    });
    camLayout->addWidget(myCameraZoomSlider);

    // Quick Lens Chips row
    QHBoxLayout* chipsLayout = new QHBoxLayout();
    chipsLayout->setSpacing(3);
    const QVector<int> lenses = {18, 24, 35, 50, 85, 135};
    for (int mm : lenses) {
        QPushButton* chip = new QPushButton(QString::number(mm), myCameraInspectorBox);
        chip->setCursor(Qt::PointingHandCursor);
        chip->setStyleSheet("QPushButton { background: #1c2128; color: #8b949e; border: 1px solid #30363d; "
                            "border-radius: 4px; padding: 2px 0px; font-size: 10px; font-weight: 600; min-height: 18px; } "
                            "QPushButton:hover { background: #262c36; color: #58a6ff; border-color: #58a6ff; }");
        chip->setToolTip(QString("%1mm prime lens").arg(mm));
        connect(chip, &QPushButton::clicked, this, [this, mm]() {
            if (myCameraZoomSlider) {
                myCameraZoomSlider->setValue(mm);
            }
        });
        chipsLayout->addWidget(chip, 1);
    }
    camLayout->addLayout(chipsLayout);

    // Aspect & Sensor (2-column compact row)
    QHBoxLayout* optRow = new QHBoxLayout();
    optRow->setSpacing(6);

    QLabel* aspLbl = new QLabel("Aspect", myCameraInspectorBox);
    aspLbl->setFixedWidth(38);
    aspLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    optRow->addWidget(aspLbl);

    myCameraAspectCombo = new QComboBox(myCameraInspectorBox);
    myCameraAspectCombo->addItem("16:9", "16:9");
    myCameraAspectCombo->addItem("2.39:1", "2.39:1");
    myCameraAspectCombo->addItem("9:16", "9:16");
    myCameraAspectCombo->addItem("1:1", "1:1");
    connect(myCameraAspectCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int) {
        if (myUsdViewport && myCameraAspectCombo) {
            myUsdViewport->setCameraAspectRatio(myCameraAspectCombo->currentData().toString());
        }
    });
    optRow->addWidget(myCameraAspectCombo, 1);

    QLabel* sensLbl = new QLabel("Sensor", myCameraInspectorBox);
    sensLbl->setFixedWidth(38);
    sensLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    optRow->addWidget(sensLbl);

    myCameraSensorCombo = new QComboBox(myCameraInspectorBox);
    myCameraSensorCombo->addItem("16:9 Dig", static_cast<int>(CameraSensorPreset::Standard16_9));
    myCameraSensorCombo->addItem("Super 35", static_cast<int>(CameraSensorPreset::Super35));
    myCameraSensorCombo->addItem("FullFrame", static_cast<int>(CameraSensorPreset::FullFrame));
    connect(myCameraSensorCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int idx) {
        if (idx >= 0 && myUsdViewport && myUsdViewport->cameraTools()) {
            myUsdViewport->cameraTools()->rigData().sensorPreset =
                static_cast<CameraSensorPreset>(myCameraSensorCombo->currentData().toInt());
            myUsdViewport->bakeCurrentCameraRig();
        }
    });
    optRow->addWidget(myCameraSensorCombo, 1);
    camLayout->addLayout(optRow);

    // Aim row
    QHBoxLayout* aimRow = new QHBoxLayout();
    aimRow->setSpacing(6);
    QLabel* aimLbl = new QLabel("Aim", myCameraInspectorBox);
    aimLbl->setFixedWidth(38);
    aimLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    aimRow->addWidget(aimLbl);

    myCameraAimModeCombo = new QComboBox(myCameraInspectorBox);
    myCameraAimModeCombo->addItem("Look-At", static_cast<int>(CameraAimMode::LookAt));
    myCameraAimModeCombo->addItem("Free Aim", static_cast<int>(CameraAimMode::Free));
    myCameraAimModeCombo->addItem("Auto-Frame", static_cast<int>(CameraAimMode::Frame));
    myCameraAimModeCombo->addItem("Tangent", static_cast<int>(CameraAimMode::PathTangent));
    connect(myCameraAimModeCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int idx) {
        if (idx >= 0 && myUsdViewport && myUsdViewport->cameraTools()) {
            myUsdViewport->cameraTools()->rigData().aimMode =
                static_cast<CameraAimMode>(myCameraAimModeCombo->currentData().toInt());
            myUsdViewport->bakeCurrentCameraRig();
        }
    });
    aimRow->addWidget(myCameraAimModeCombo, 1);
    camLayout->addLayout(aimRow);

    // Target row
    QHBoxLayout* targetRow = new QHBoxLayout();
    targetRow->setSpacing(6);
    QLabel* tgtLbl = new QLabel("Target", myCameraInspectorBox);
    tgtLbl->setFixedWidth(38);
    tgtLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    targetRow->addWidget(tgtLbl);

    myCameraAimTargetCombo = new QComboBox(myCameraInspectorBox);
    myCameraAimTargetCombo->addItem("Movable Look-At Arrow", QString());
    connect(myCameraAimTargetCombo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this](int idx) {
        if (idx >= 0 && myUsdViewport && myUsdViewport->cameraTools()) {
            const QString targetPath = myCameraAimTargetCombo->currentData().toString();
            CameraRigData& rig = myUsdViewport->cameraTools()->rigData();
            rig.aimTargets.clear();
            if (!targetPath.isEmpty()) {
                rig.aimTargets.append(targetPath);
                rig.shotSubject = targetPath;
            } else {
                rig.shotSubject.clear();
            }
            myUsdViewport->bakeCurrentCameraRig();
        }
    });
    targetRow->addWidget(myCameraAimTargetCombo, 1);

    myCenterAimButton = new QPushButton("Center", myCameraInspectorBox);
    myCenterAimButton->setCursor(Qt::PointingHandCursor);
    myCenterAimButton->setToolTip("Center the green camera look-at arrow to the middle of the shot/stage");
    myCenterAimButton->setStyleSheet("QPushButton { background: #1c2128; color: #34d399; border: 1px solid #059669; "
                                     "border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: 600; min-height: 20px; } "
                                     "QPushButton:hover { background: #064e3b; color: #6ee7b7; border-color: #34d399; }");
    connect(myCenterAimButton, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->resetAimTargetToCenter();
            updateCameraInspectorUi();
            if (myCanvasStatus) {
                myCanvasStatus->setText("Centered camera look-at arrow to middle of the shot");
            }
        }
    });
    targetRow->addWidget(myCenterAimButton);
    camLayout->addLayout(targetRow);

    // Checkboxes in one compact row
    QHBoxLayout* checksRow = new QHBoxLayout();
    checksRow->setSpacing(10);
    myCameraFramingModeCheck = new QCheckBox("Dolly framing", myCameraInspectorBox);
    myCameraFramingModeCheck->setChecked(true);
    myCameraFramingModeCheck->setToolTip("Checked = move camera physically; Unchecked = zoom lens");
    connect(myCameraFramingModeCheck, &QCheckBox::toggled, this, [this](bool checked) {
        if (myUsdViewport && myUsdViewport->cameraTools()) {
            myUsdViewport->cameraTools()->rigData().framingMoveCamera = checked;
        }
    });
    checksRow->addWidget(myCameraFramingModeCheck);

    myDollyZoomCheck = new QCheckBox("Vertigo effect", myCameraInspectorBox);
    myDollyZoomCheck->setToolTip("Automatic dolly zoom maintaining subject framing");
    connect(myDollyZoomCheck, &QCheckBox::toggled, this, [this](bool checked) {
        if (myUsdViewport && myUsdViewport->cameraTools()) {
            myUsdViewport->cameraTools()->rigData().dollyZoomEnabled = checked;
            myUsdViewport->bakeCurrentCameraRig();
        }
    });
    checksRow->addWidget(myDollyZoomCheck);
    camLayout->addLayout(checksRow);

    // Camera Path & Joints section
    QFrame* pathSep = new QFrame(myCameraInspectorBox);
    pathSep->setFrameShape(QFrame::HLine);
    pathSep->setStyleSheet("color: #30363d; margin: 4px 0px;");
    camLayout->addWidget(pathSep);

    QHBoxLayout* pathHeaderRow = new QHBoxLayout();
    pathHeaderRow->setContentsMargins(0, 0, 0, 0);
    QLabel* pathTitleLbl = new QLabel("Camera Path", myCameraInspectorBox);
    pathTitleLbl->setStyleSheet("font-size: 11px; font-weight: 600; color: #8b949e;");
    pathHeaderRow->addWidget(pathTitleLbl);
    pathHeaderRow->addStretch();
    myPathJointsCountLabel = new QLabel("0 Joints", myCameraInspectorBox);
    myPathJointsCountLabel->setStyleSheet("font-size: 11px; font-weight: 700; color: #38bdf8;");
    pathHeaderRow->addWidget(myPathJointsCountLabel);
    camLayout->addLayout(pathHeaderRow);

    QHBoxLayout* pathButtonsRow = new QHBoxLayout();
    pathButtonsRow->setSpacing(5);

    myCameraDrawPathButton = new QPushButton("✏️ Draw", myCameraInspectorBox);
    myCameraDrawPathButton->setCheckable(true);
    myCameraDrawPathButton->setCursor(Qt::PointingHandCursor);
    myCameraDrawPathButton->setToolTip("Sketch camera movement path directly in the 3D viewport (Click & drag in 3D)");
    myCameraDrawPathButton->setStyleSheet(
        "QPushButton { background: #1c2128; color: #c9d1d9; border: 1px solid #30363d; "
        "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 600; min-height: 22px; } "
        "QPushButton:hover { background: #21262d; border-color: #58a6ff; color: #ffffff; } "
        "QPushButton:checked { background: #1a365d; border: 1.5px solid #388bfd; color: #58a6ff; font-weight: 700; } ");
    connect(myCameraDrawPathButton, &QPushButton::toggled, this, [this](bool checked) {
        if (!myUsdViewport) return;
        if (checked) {
            myUsdViewport->startDrawingPath(QStringLiteral("/World/ShotCam"));
        } else {
            myUsdViewport->stopDrawingPath();
        }
        updateCameraInspectorUi();
        updatePrimPathControls();
    });
    pathButtonsRow->addWidget(myCameraDrawPathButton, 1);

    myExtendPathButton = new QPushButton("➕ Extend", myCameraInspectorBox);
    myExtendPathButton->setCursor(Qt::PointingHandCursor);
    myExtendPathButton->setToolTip("Extend the camera path with a new joint continuing along its trajectory (Hotkey: E)");
    myExtendPathButton->setStyleSheet("QPushButton { background: #1c2128; color: #38bdf8; border: 1px solid #0284c7; "
                                      "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 600; min-height: 22px; } "
                                      "QPushButton:hover { background: #0c4a6e; color: #7dd3fc; border-color: #38bdf8; }");
    connect(myExtendPathButton, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->extendCameraPath();
            updateCameraInspectorUi();
        }
    });
    pathButtonsRow->addWidget(myExtendPathButton, 1);

    myDeleteLastJointButton = new QPushButton("➖ Delete Last", myCameraInspectorBox);
    myDeleteLastJointButton->setCursor(Qt::PointingHandCursor);
    myDeleteLastJointButton->setToolTip("Delete the last joint from the camera path (or Backspace to delete selected joint)");
    myDeleteLastJointButton->setStyleSheet("QPushButton { background: #1c2128; color: #f87171; border: 1px solid #dc2626; "
                                           "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 600; min-height: 22px; } "
                                           "QPushButton:hover { background: #450a0a; color: #fca5a5; border-color: #ef4444; }");
    connect(myDeleteLastJointButton, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->deleteLastCameraPathJoint();
            updateCameraInspectorUi();
        }
    });
    pathButtonsRow->addWidget(myDeleteLastJointButton, 1);

    myClearPathButton = new QPushButton("🗑️ Clear Path", myCameraInspectorBox);
    myClearPathButton->setCursor(Qt::PointingHandCursor);
    myClearPathButton->setToolTip("Clear the camera path as a whole");
    myClearPathButton->setStyleSheet("QPushButton { background: #1c2128; color: #94a3b8; border: 1px solid #475569; "
                                     "border-radius: 4px; padding: 4px 6px; font-size: 10px; font-weight: 600; min-height: 22px; } "
                                     "QPushButton:hover { background: #1e293b; color: #cbd5e1; border-color: #94a3b8; }");
    connect(myClearPathButton, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->clearCameraPath();
            updateCameraInspectorUi();
        }
    });
    pathButtonsRow->addWidget(myClearPathButton, 1);

    camLayout->addLayout(pathButtonsRow);

    // Action buttons in one compact row
    QHBoxLayout* actionsRow = new QHBoxLayout();
    actionsRow->setSpacing(6);
    QPushButton* convertLlmBtn = new QPushButton("Convert LLM → Rig", myCameraInspectorBox);
    convertLlmBtn->setCursor(Qt::PointingHandCursor);
    convertLlmBtn->setToolTip("Sample LLM ShotCam into an editable Catmull-Rom spline rig");
    convertLlmBtn->setStyleSheet("QPushButton { background: #1c2128; color: #58a6ff; border: 1px solid #388bfd; "
                                 "border-radius: 5px; padding: 5px; font-size: 11px; font-weight: 600; min-height: 24px; } "
                                 "QPushButton:hover { background: #21262d; border-color: #79c0ff; color: #79c0ff; }");
    connect(convertLlmBtn, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->convertLlmCameraToManualRig();
            updateCameraInspectorUi();
            if (myCanvasStatus) {
                myCanvasStatus->setText("Converted LLM Camera to editable Director Rig in user_edits.usda");
            }
        }
    });
    actionsRow->addWidget(convertLlmBtn, 1);

    myResetCameraEditsButton = new QPushButton("Reset Edits", myCameraInspectorBox);
    myResetCameraEditsButton->setCursor(Qt::PointingHandCursor);
    myResetCameraEditsButton->setObjectName("secondaryButton");
    myResetCameraEditsButton->setStyleSheet("QPushButton { background: #1c2128; color: #8b949e; border: 1px solid #30363d; "
                                            "border-radius: 5px; padding: 5px; font-size: 11px; font-weight: 600; min-height: 24px; } "
                                            "QPushButton:hover { background: #262c36; color: #c9d1d9; border-color: #8b949e; }");
    connect(myResetCameraEditsButton, &QPushButton::clicked, this, [this]() {
        if (myUsdViewport) {
            myUsdViewport->resetCameraEdits();
            updateCameraInspectorUi();
            if (myCanvasStatus) {
                myCanvasStatus->setText("Reset camera edits to shot default");
            }
        }
    });
    actionsRow->addWidget(myResetCameraEditsButton, 1);
    camLayout->addLayout(actionsRow);

    camLayout->addStretch(1);
    parentLayout->addWidget(myCameraInspectorBox);
}

void MainWindow::updateCameraInspectorUi() {
    if (!myUsdViewport) return;

    // 1. Viewfinder / 3D World toggle button text & style
    if (myCameraViewModeButton) {
        const bool isCam = (myUsdViewport->viewMode() == ViewMode::CameraViewfinder);
        myCameraViewModeButton->setText(isCam
            ? "🌐 Switch to 3D World Editor • Tab"
            : "🎥 Switch to Camera Viewfinder • Tab");
        myCameraViewModeButton->setChecked(isCam);
        myCameraViewModeButton->setStyleSheet(isCam
            ? "QPushButton { background: #1c2128; color: #58a6ff; border: 1px solid #388bfd; "
              "font-weight: 600; border-radius: 6px; padding: 6px 10px; font-size: 11px; } "
              "QPushButton:hover { background: #262c36; color: #79c0ff; border-color: #79c0ff; }"
            : "QPushButton { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #2563eb, stop:1 #1d4ed8); "
              "color: #ffffff; border: 1px solid #60a5fa; font-weight: bold; border-radius: 6px; padding: 6px 10px; font-size: 11px; } "
              "QPushButton:hover { background: #3b82f6; }");
        if (myCameraInspectorBox) {
            myCameraInspectorBox->setTitle(isCam ? "CAMERA DIRECTOR • [VIEWFINDER]" : "CAMERA DIRECTOR • [3D WORLD]");
        }
    }

    // 2. Focal length & Zoom
    const double focal = myUsdViewport->cameraFocalLength();
    if (myCameraZoomSlider) {
        QSignalBlocker blocker(myCameraZoomSlider);
        myCameraZoomSlider->setValue(qBound(12, static_cast<int>(std::round(focal)), 200));
    }
    if (myCameraZoomValueLabel) {
        myCameraZoomValueLabel->setText(QString("%1 mm").arg(static_cast<int>(std::round(focal))));
    }

    // 4. Aspect Ratio
    const QString aspect = myUsdViewport->cameraAspectRatio();
    if (myCameraAspectCombo) {
        QSignalBlocker blocker(myCameraAspectCombo);
        for (int i = 0; i < myCameraAspectCombo->count(); ++i) {
            if (myCameraAspectCombo->itemData(i).toString() == aspect) {
                myCameraAspectCombo->setCurrentIndex(i);
                break;
            }
        }
    }

    // 5. Shot Size
    const CameraShotSize sz = myUsdViewport->shotSize();
    if (myCameraShotSizeCombo) {
        QSignalBlocker blocker(myCameraShotSizeCombo);
        for (int i = 0; i < myCameraShotSizeCombo->count(); ++i) {
            if (myCameraShotSizeCombo->itemData(i).toInt() == static_cast<int>(sz)) {
                myCameraShotSizeCombo->setCurrentIndex(i);
                break;
            }
        }
    }

    // 6. Camera Rig Data
    if (myUsdViewport->cameraTools()) {
        const CameraRigData& rig = myUsdViewport->cameraTools()->rigData();
        if (myCameraFramingModeCheck) {
            QSignalBlocker blocker(myCameraFramingModeCheck);
            myCameraFramingModeCheck->setChecked(rig.framingMoveCamera);
        }
        if (myDollyZoomCheck) {
            QSignalBlocker blocker(myDollyZoomCheck);
            myDollyZoomCheck->setChecked(rig.dollyZoomEnabled);
        }
        if (myCameraSensorCombo) {
            QSignalBlocker blocker(myCameraSensorCombo);
            for (int i = 0; i < myCameraSensorCombo->count(); ++i) {
                if (myCameraSensorCombo->itemData(i).toInt() == static_cast<int>(rig.sensorPreset)) {
                    myCameraSensorCombo->setCurrentIndex(i);
                    break;
                }
            }
        }
        if (myCameraAimModeCombo) {
            QSignalBlocker blocker(myCameraAimModeCombo);
            for (int i = 0; i < myCameraAimModeCombo->count(); ++i) {
                if (myCameraAimModeCombo->itemData(i).toInt() == static_cast<int>(rig.aimMode)) {
                    myCameraAimModeCombo->setCurrentIndex(i);
                    break;
                }
            }
        }

        if (myCameraAimTargetCombo) {
            QSignalBlocker blocker(myCameraAimTargetCombo);
            const QString currentTarget = rig.shotSubject.isEmpty()
                ? (rig.aimTargets.isEmpty() ? QString() : rig.aimTargets.first())
                : rig.shotSubject;

            myCameraAimTargetCombo->clear();
            myCameraAimTargetCombo->addItem("Movable Look-At Arrow", QString());
            const QVector<UsdPrimItem> entries = myUsdViewport->primList();
            int selectedIdx = 0;
            for (const UsdPrimItem& item : entries) {
                if (item.path == QStringLiteral("/World/ShotCam") || item.path.startsWith("/World/CameraRig")) {
                    continue;
                }
                const QString label = !item.actorLabel.isEmpty() ? item.actorLabel : item.name;
                myCameraAimTargetCombo->addItem(label, item.path);
                if (item.path == currentTarget) {
                    selectedIdx = myCameraAimTargetCombo->count() - 1;
                }
            }
            myCameraAimTargetCombo->setCurrentIndex(selectedIdx);
        }

        if (myPathJointsCountLabel) {
            const int count = myUsdViewport->cameraPathJointCount();
            myPathJointsCountLabel->setText(QString("%1 Joint%2").arg(count).arg(count == 1 ? "" : "s"));
            if (myDeleteLastJointButton) {
                myDeleteLastJointButton->setEnabled(count > 0);
            }
            if (myClearPathButton) {
                myClearPathButton->setEnabled(count > 1);
            }
        }

        if (myCameraDrawPathButton) {
            const bool isDrawingCam = (myUsdViewport->isDrawPathActive() &&
                (myUsdViewport->drawingPrimPath() == QStringLiteral("/World/ShotCam") ||
                 myUsdViewport->drawingPrimPath() == myUsdViewport->activeCameraPath()));
            QSignalBlocker b(myCameraDrawPathButton);
            myCameraDrawPathButton->setChecked(isDrawingCam);
            myCameraDrawPathButton->setText(isDrawingCam ? "✏️ Drawing…" : "✏️ Draw");
        }
    }
}

void MainWindow::updateSelectionScale(double scale) {
    if (myUsdViewport && !myUsdViewport->selectedPrimPath().isEmpty()) {
        myUsdViewport->setSelectedPrimScale(scale);
    }
}

void MainWindow::updateSelectionRotation(double) {
    if (myUsdViewport && !myUsdViewport->selectedPrimPath().isEmpty()) {
        myUsdViewport->setSelectedPrimRotation(QVector3D(
            static_cast<float>(myRotationX->value()),
            static_cast<float>(myRotationY->value()),
            static_cast<float>(myRotationZ->value())));
    }
}

void MainWindow::positionCoordinateChanged(double) {
    if (myUsdViewport && !myUsdViewport->selectedPrimPath().isEmpty()) {
        myUsdViewport->setSelectedPrimPosition(QVector3D(
            static_cast<float>(myPositionX->value()),
            static_cast<float>(myPositionY->value()),
            static_cast<float>(myPositionZ->value())));
    }
}

void MainWindow::removeSelectedAsset() {
    if (myUsdViewport) {
        myUsdViewport->deleteSelectedPrim();
    }
}

void MainWindow::newConcept() {
    if (myUsdViewport) {
        myUsdViewport->resetFreeCamera();
        myCanvasStatus->setText("USD camera reset to default view");
    }
}

void MainWindow::renderScene() {
    if (myCurrentScene < 0 || myCurrentPanel < 0 || myCurrentPanel >= storyboardPanels().size()) {
        startStoryboardPanel();
    }
    if (myCurrentScene < 0 || myCurrentPanel < 0 || myCurrentPanel >= storyboardPanels().size()) return;

    if (myUsdViewport) {
        myUsdViewport->bakeCurrentCameraRig();
    }

    const QImage renderedView = myUsdViewport
        ? myUsdViewport->renderCurrentView()
        : QImage();
    if (renderedView.isNull()) {
        QMessageBox::warning(this, "Render failed", "The storyboard panel could not be rendered.");
        return;
    }
    const QString stagePath = ensureShotStageFile(myCurrentScene, myCurrentPanel);
    const QFileInfo stageInfo(stagePath);
    renderedView.save(stageInfo.dir().filePath("thumbnail.jpg"), "JPG", 90);
    saveShotActions(myCurrentScene, myCurrentPanel);

    const QString projectRoot = painterSpaceProjectRoot();
    const QString script = QDir(projectRoot).filePath("tools/world_state.py");
    const QString python = worldStatePythonFor(projectRoot);
    if (QFileInfo::exists(script) && !python.isEmpty()) {
        QProcess proc;
        proc.setWorkingDirectory(projectRoot);
        proc.start(python, {script, QStringLiteral("finalize-shot"), stagePath, QStringLiteral("--no-images")});
        proc.waitForFinished(30000);
    }

    storyboardPanels()[myCurrentPanel].usdStagePath = stagePath;
    storyboardPanels()[myCurrentPanel].render = renderedView;
    if (myScenePrompt) storyboardPanels()[myCurrentPanel].prompt = myScenePrompt->toPlainText();
    myLastEditedPanel = myCurrentPanel;
    showStoryboardBook();
}

void MainWindow::useUsdAssetInPrompt(const QString& assetPath) {
    if (!myScenePrompt) return;
    QString assetName;
    QString clipName;
    QString assetId;
    if (assetPath.endsWith("running_fast.usdc")) {
        assetName = "running human";
        clipName = "running";
        assetId = "running_fast";
    } else if (assetPath.endsWith("walking.usdc")) {
        assetName = "walking human";
        clipName = "walking";
        assetId = "walking";
    } else {
        return;
    }

    QString effectiveAssetPath = assetPath;
    if (myCurrentPanel < 0 && effectiveAssetPath.startsWith("../../../assets/")) {
        effectiveAssetPath = QStringLiteral("../../assets/") + effectiveAssetPath.mid(16);
    }

    if (myUsdViewport) {
        myUsdViewport->addReferencedAsset(assetId, effectiveAssetPath, "Human", "+Z", 1.0);
    }

    QString prompt = myScenePrompt->toPlainText().trimmed();
    if (!prompt.contains(effectiveAssetPath)) {
        if (!prompt.isEmpty()) prompt += "\n";
        prompt += QString("Use the rigged %1 asset @%2@ and its embedded mixamo_com %3 animation clip.")
                      .arg(assetName, effectiveAssetPath, clipName);
        myScenePrompt->setPlainText(prompt);
    }
    myScenePrompt->setFocus();
}

void MainWindow::generateUsdForCurrentPanel() {
    if (myUsdGenerationProcess) return;
    if (myCurrentPanel >= 0) {
        saveShotActions(myCurrentScene, myCurrentPanel);
        const auto info = runDirectorTool({"--brief-info"});
        if (info.contains("error")) { myCanvasStatus->setText(info["error"].toString()); return; }
        const auto lint = info["lint"].toArray();
        if (!lint.isEmpty()) {
            const auto firstIssue = lint.first().toObject();
            const QString code = firstIssue["code"].toString();
            QString explanation;
            if (code == QStringLiteral("ACTION_SPEED_MISMATCH")) {
                const QString segmentId = firstIssue["segment"].toString();
                const auto tracks = info["brief"].toObject()["tracks"].toObject();
                for (auto track = tracks.begin(); track != tracks.end(); ++track) {
                    for (const auto& value : track.value().toObject()["segments"].toArray()) {
                        const auto segment = value.toObject();
                        if (segment["id"].toString() != segmentId) continue;
                        explanation = QString("%1's %2 action requires %3 m/s along its route, outside the allowed speed for that action.")
                                          .arg(track.key(), segment["action"].toString(),
                                               QString::number(firstIssue["speed_mps"].toDouble(), 'f', 2));
                        break;
                    }
                    if (!explanation.isEmpty()) break;
                }
            }
            if (explanation.isEmpty()) explanation = code.toLower().replace('_', ' ');
            const QString message = QString("The shot was not queued because its brief has %1 issue%2.\n\n%3\n\nUse ‘What the AI will see · Check brief’ to review the issue and available fixes.")
                                        .arg(lint.size()).arg(lint.size() == 1 ? "" : "s").arg(explanation);
            myCanvasStatus->setText(QStringLiteral("Shot not queued: resolve the brief issue first"));
            QMessageBox::warning(this, QStringLiteral("Shot not queued"), message);
            return;
        }
    }
    const bool agentMode = qgetenv("OPENAI_API_KEY").isEmpty() ||
                           qEnvironmentVariable("PAINTERSPACE_LLM_PROVIDER") == "agent" ||
                           qEnvironmentVariable("PAINTERSPACE_AGENT_MODE") == "1";
    if (!agentMode && qEnvironmentVariableIsEmpty("OPENAI_API_KEY")) {
        QMessageBox::warning(this, "API key missing",
                             "Set OPENAI_API_KEY in the environment before launching PainterSpace to generate a USD scene.");
        return;
    }

    QString prompt = myScenePrompt ? myScenePrompt->toPlainText().trimmed() : QString();
    if (myCurrentPanel >= 0) {
        saveShotActions(myCurrentScene, myCurrentPanel);
    }
    const QVector<ActionSegment> timelineActions = myActionTimeline ? myActionTimeline->actions() : QVector<ActionSegment>{};
    const bool hasTimelineInput = myCurrentPanel >= 0 &&
        std::any_of(timelineActions.cbegin(), timelineActions.cend(), [](const ActionSegment& action) {
            return !action.generated && action.id != "render_only_camera";
        });
    const bool hasDrawnPathInput = myCurrentPanel >= 0 && myUsdViewport &&
        !myUsdViewport->explicitlyDrawnPrimPaths().isEmpty();
    if (prompt.isEmpty() && !hasTimelineInput && !hasDrawnPathInput) {
        QMessageBox::information(this, "Describe the scene or shot", "Enter a prompt, draw an actor path, or add an action timeline segment before generating a USD stage.");
        if (myScenePrompt) myScenePrompt->setFocus();
        return;
    }

    if (myCurrentScene < 0 || myCurrentScene >= myStoryboardScenes.size()) {
        QMessageBox::warning(this, "No scene selected", "Select a scene before generating USD.");
        return;
    }

    const QString projectRoot = painterSpaceProjectRoot();

    // SCENE SETUP MODE (Step 1: Describe Scene before moving to shots)
    if (myCurrentPanel < 0) {
        const int sceneIndex = myCurrentScene;
        const QString relativeStagePath = QString("scenes/scene_%1/scene.usda")
                                              .arg(sceneIndex + 1, 2, 10, QChar('0'));
        const QString stagePath = QDir(projectRoot).absoluteFilePath(relativeStagePath);

        StoryboardScene& scene = myStoryboardScenes[sceneIndex];
        scene.prompt = prompt;
        scene.usdStagePath = stagePath;

        myCanvasStatus->setText(QString("Generating shared USD scene for %1…").arg(scene.name));
        if (myUsdViewport) {
            myUsdViewport->setGenerationStatus(
                QString("LLM generating %1 (scene.usda)…").arg(scene.name), false, true);
        }
        myGenerateUsdButton->setEnabled(false);
        myUsdGenerationProcess = new QProcess(this);
        myUsdGenerationProcess->setWorkingDirectory(projectRoot);
        QProcess* process = myUsdGenerationProcess;
        connect(process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
                [this, process, sceneIndex, prompt, stagePath, relativeStagePath](int exitCode, QProcess::ExitStatus exitStatus) {
            if (myUsdGenerationProcess != process) return;
            myUsdGenerationProcess = nullptr;
            myGenerateUsdButton->setEnabled(true);
            if (myUsdViewport) myUsdViewport->setGenerationStatus(QString());
            const QString output = QString::fromUtf8(process->readAllStandardOutput()).trimmed();
            const QString errors = QString::fromUtf8(process->readAllStandardError()).trimmed();
            process->deleteLater();

            if (output.contains("[AGENT_DISPATCH]")) {
                if (sceneIndex >= 0 && sceneIndex < myStoryboardScenes.size()) {
                    myStoryboardScenes[sceneIndex].prompt = prompt;
                }
                myCanvasStatus->setText("Prompt queued in task_queue/pending_request.json • Waiting for LLM tools call JSON in task_queue/llm_response.json (execution ends there)…");
                if (myUsdViewport) {
                    myUsdViewport->setGenerationStatus("Waiting for IDE LLM tools call JSON response (task_queue/llm_response.json, execution ends there)…", false, false);
                }
                QMessageBox::information(this, "Prompt Queued for IDE LLM Endpoint",
                    QString("Your scene prompt has been queued in task_queue/pending_request.json!\n\n"
                            "Target: %1\n"
                            "Prompt: \"%2\"\n\n"
                            "In IDE chat, prompt the LLM:\n"
                            "  \"Respond to the queued task: output ONLY the tools call JSON to task_queue/llm_response.json, and end execution there.\"\n\n"
                            "The LLM must ONLY respond with the tools call JSON and execution must end there. "
                            "PainterSpace will automatically execute the function calls and compile the USD stage itself.")
                        .arg(relativeStagePath, prompt));
                return;
            }

            if (exitStatus != QProcess::NormalExit || exitCode != 0) {
                const QString reason = !errors.isEmpty() ? errors : output;
                if (myUsdViewport) {
                    myUsdViewport->setGenerationStatus(
                        "Scene USD generation failed — keeping last valid stage", true, false);
                }
                myCanvasStatus->setText("Scene USD generation failed  •  keeping previous stage");
                QMessageBox::warning(this, "Scene USD generation failed",
                                     reason.isEmpty() ? "The generator exited before producing a scene stage." : reason);
                return;
            }

            if (sceneIndex >= 0 && sceneIndex < myStoryboardScenes.size()) {
                StoryboardScene& s = myStoryboardScenes[sceneIndex];
                s.prompt = prompt;
                s.usdStagePath = stagePath;
                s.configured = true;
                if (myUsdViewport) {
                    if (myUsdViewport->stagePath() == stagePath) {
                        myUsdViewport->reloadStage();
                    } else {
                        myUsdViewport->openStage(stagePath, true);
                    }
                    const QImage renderedView = myUsdViewport->renderCurrentView();
                    if (!renderedView.isNull()) {
                        s.render = renderedView;
                        renderedView.save(QFileInfo(stagePath).dir().filePath("thumbnail.jpg"), "JPG", 90);
                    }
                }
                rebuildStoryboardScenes();
            }
            myCanvasStatus->setText("Scene USD generated & live-reloaded in Hydra viewport");
        });
        connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError error) {
            if (error != QProcess::FailedToStart || myUsdGenerationProcess != process) return;
            myUsdGenerationProcess = nullptr;
            myGenerateUsdButton->setEnabled(true);
            if (myUsdViewport) myUsdViewport->setGenerationStatus(QString());
            myCanvasStatus->setText("Unable to start the USDA generator");
            QMessageBox::warning(this, "USD generation failed",
                                 "PainterSpace could not start its USDA generation command.");
            process->deleteLater();
        });
        process->start(QCoreApplication::applicationFilePath(), {"--generate-usda", prompt, relativeStagePath});
        return;
    }

    // SHOT EDITOR MODE (Step 2: Individual Shots)
    if (myCurrentPanel >= storyboardPanels().size() || !storyboardPanels().at(myCurrentPanel).active) {
        startStoryboardPanel();
        if (myScenePrompt && myScenePrompt->toPlainText().isEmpty()) myScenePrompt->setPlainText(prompt);
    }
    if (myCurrentPanel < 0 || myCurrentPanel >= storyboardPanels().size() ||
        !storyboardPanels().at(myCurrentPanel).active) {
        QMessageBox::warning(this, "No storyboard panel available",
                             "Create or open a storyboard panel before generating a shot.");
        return;
    }

    const int panelIndex = myCurrentPanel;
    const QString relativeStagePath = QString("scenes/scene_%1/shot_%2/shot.usda")
                                          .arg(myCurrentScene + 1, 2, 10, QChar('0'))
                                          .arg(panelIndex + 1, 3, 10, QChar('0'));
    const QString stagePath = QDir(projectRoot).absoluteFilePath(relativeStagePath);

    storyboardPanels()[panelIndex].prompt = prompt;
    storyboardPanels()[panelIndex].usdStagePath = stagePath;

    myCanvasStatus->setText(QString("Generating USD for %1…").arg(storyboardPanels().at(panelIndex).name));
    if (myUsdViewport) {
        myUsdViewport->setGenerationStatus(
            QString("LLM generating %1 (shot.usda)…").arg(storyboardPanels().at(panelIndex).name), false, true);
    }
    myGenerateUsdButton->setEnabled(false);
    myUsdGenerationProcess = new QProcess(this);
    myUsdGenerationProcess->setWorkingDirectory(projectRoot);
    QProcess* process = myUsdGenerationProcess;
    connect(process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, process, panelIndex, prompt, stagePath, relativeStagePath](int exitCode, QProcess::ExitStatus exitStatus) {
        if (myUsdGenerationProcess != process) return;
        myUsdGenerationProcess = nullptr;
        myGenerateUsdButton->setEnabled(true);
        if (myUsdViewport) myUsdViewport->setGenerationStatus(QString());
        const QString output = QString::fromUtf8(process->readAllStandardOutput()).trimmed();
        const QString errors = QString::fromUtf8(process->readAllStandardError()).trimmed();
        process->deleteLater();

        if (output.contains("[AGENT_DISPATCH]")) {
            if (panelIndex >= 0 && panelIndex < storyboardPanels().size() && storyboardPanels().at(panelIndex).active) {
                storyboardPanels()[panelIndex].prompt = prompt;
            }
            myCanvasStatus->setText("Prompt queued in task_queue/pending_request.json • Waiting for LLM tools call JSON in task_queue/llm_response.json (execution ends there)…");
            if (myUsdViewport) {
                myUsdViewport->setGenerationStatus("Waiting for IDE LLM tools call JSON response (task_queue/llm_response.json, execution ends there)…", false, false);
            }
            QMessageBox::information(this, "Prompt Queued for IDE LLM Endpoint",
                QString("Your shot prompt has been queued in task_queue/pending_request.json!\n\n"
                        "Target: %1\n"
                        "Prompt: \"%2\"\n\n"
                        "In IDE chat, prompt the LLM:\n"
                        "  \"Respond to the queued task: output ONLY the tools call JSON to task_queue/llm_response.json, and end execution there.\"\n\n"
                        "The LLM must ONLY respond with the tools call JSON and execution must end there. "
                        "PainterSpace will automatically execute the function calls, validate collisions, and compile the USD stage itself.")
                    .arg(relativeStagePath, prompt));
            return;
        }

        if (exitStatus != QProcess::NormalExit || exitCode != 0) {
            const QString reason = !errors.isEmpty() ? errors : output;
            if (myUsdViewport) {
                myUsdViewport->setGenerationStatus(
                    "Shot USD generation failed — keeping last valid stage", true, false);
            }
            myCanvasStatus->setText("USD generation failed  •  keeping previous shot stage");
            QMessageBox::warning(this, "USD generation failed",
                                 reason.isEmpty() ? "The generator exited before producing a stage." : reason);
            return;
        }

        if (panelIndex >= 0 && panelIndex < storyboardPanels().size() && storyboardPanels().at(panelIndex).active) {
            storyboardPanels()[panelIndex].prompt = prompt;
            storyboardPanels()[panelIndex].usdStagePath = stagePath;
            if (myUsdViewport) {
                if (myUsdViewport->stagePath() == stagePath) {
                    myUsdViewport->reloadStage();
                } else {
                    myUsdViewport->openStage(stagePath);
                }
                refreshGeneratedShotTimeline(stagePath);
                const QImage preview = myUsdViewport->renderCurrentView();
                if (!preview.isNull()) {
                    storyboardPanels()[panelIndex].render = preview;
                    preview.save(QFileInfo(stagePath).dir().filePath("thumbnail.jpg"), "JPG", 90);
                }
                myLastEditedPanel = panelIndex;
                syncLayers();
                updateSelectionPanel();
                myCanvasStatus->setText("Shot USD generated & live-reloaded in Hydra viewport (camera & user_edits preserved)");
                return;
            }
        }
        myCanvasStatus->setText("USD generated and validated • rendering preview…");
        refreshGeneratedShotTimeline(stagePath);
        renderAndPlayUsdStageFile(stagePath, true);
    });
    connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError error) {
        if (error != QProcess::FailedToStart || myUsdGenerationProcess != process) return;
        myUsdGenerationProcess = nullptr;
        myGenerateUsdButton->setEnabled(true);
        if (myUsdViewport) myUsdViewport->setGenerationStatus(QString());
        myCanvasStatus->setText("Unable to start the USDA generator");
        QMessageBox::warning(this, "USD generation failed",
                             "PainterSpace could not start its USDA generation command.");
        process->deleteLater();
    });
    QStringList genArgs{"--generate-usda", prompt, relativeStagePath};
    if (myShotMode == ShotMode::DirectorManual) {
        genArgs << QStringLiteral("--camera-locked");
    }
    if (myGenerationMode && myGenerationMode->currentText() == "Manual") {
        const QString python = QDir(projectRoot).filePath(".venv/bin/python");
        process->start(python, {QDir(projectRoot).filePath("tools/shot_library/compile_shot.py"), "--manual", "--prompt", prompt, "--out", stagePath});
    } else process->start(QCoreApplication::applicationFilePath(), genArgs);
}

void MainWindow::pollAgentLlmResponse() {
    if (myUsdGenerationProcess) return;

    const QString projectRoot = painterSpaceProjectRoot();
    const QDir rootDir(projectRoot);
    const QString responsePath = rootDir.filePath("task_queue/llm_response.json");
    if (!QFileInfo::exists(responsePath)) return;

    // Ensure the JSON file has finished being written and is valid JSON
    QFile respFile(responsePath);
    if (!respFile.open(QIODevice::ReadOnly | QIODevice::Text)) return;
    const QByteArray rawBytes = respFile.readAll().trimmed();
    respFile.close();
    if (rawBytes.isEmpty()) return;

    QJsonParseError parseErr;
    const QJsonDocument doc = QJsonDocument::fromJson(rawBytes, &parseErr);
    if (parseErr.error != QJsonParseError::NoError || (!doc.isObject() && !doc.isArray())) {
        return;
    }

    const QString pendingPath = rootDir.filePath("task_queue/pending_request.json");
    const QJsonObject pendingTask = readJsonFile(pendingPath);
    const QString relativeStagePath = pendingTask.value("target_stage").toString();
    if (relativeStagePath.isEmpty()) return;

    const QString stagePath = rootDir.absoluteFilePath(relativeStagePath);
    const QString prompt = pendingTask.value("prompt").toString();
    const bool isSceneStage = relativeStagePath.endsWith("scene.usda");
    const QString taskId = pendingTask.value("task_id").toString();

    const QString pythonBin = worldStatePythonFor(projectRoot);
    const QString compileScript = rootDir.filePath("tools/shot_library/compile_shot.py");
    if (pythonBin.isEmpty() || !QFileInfo::exists(compileScript)) return;

    myCanvasStatus->setText(QString("Executing LLM JSON function calls for %1…").arg(relativeStagePath));
    if (myUsdViewport) {
        myUsdViewport->setGenerationStatus(
            QString("Executing LLM JSON tool calls & compiling %1…").arg(relativeStagePath), false, true);
    }
    if (myGenerateUsdButton) myGenerateUsdButton->setEnabled(false);

    myUsdGenerationProcess = new QProcess(this);
    myUsdGenerationProcess->setWorkingDirectory(projectRoot);
    QProcess* process = myUsdGenerationProcess;
    connect(process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
            [this, process, stagePath, prompt, isSceneStage, taskId, projectRoot](int exitCode, QProcess::ExitStatus exitStatus) {
        if (myUsdGenerationProcess != process) return;
        myUsdGenerationProcess = nullptr;
        if (myGenerateUsdButton) myGenerateUsdButton->setEnabled(true);
        if (myUsdViewport) myUsdViewport->setGenerationStatus(QString());
        const QString output = QString::fromUtf8(process->readAllStandardOutput()).trimmed();
        const QString errors = QString::fromUtf8(process->readAllStandardError()).trimmed();
        process->deleteLater();

        if (exitStatus != QProcess::NormalExit || exitCode != 0) {
            const QString valErrPath = QDir(projectRoot).filePath("task_queue/validation_error.json");
            const QJsonObject valErr = readJsonFile(valErrPath);
            const QString errMsg = !valErr.value("message").toString().isEmpty()
                ? valErr.value("message").toString()
                : (!errors.isEmpty() ? errors : output);
            if (myUsdViewport) {
                myUsdViewport->setGenerationStatus(
                    "LLM JSON validation failed — diagnostic written to task_queue/pending_request.json", true, false);
            }
            myCanvasStatus->setText("LLM JSON execution/validation failed • Waiting for revised task_queue/llm_response.json");
            QMessageBox::warning(this, "LLM Response Validation Failed",
                QString("PainterSpace executed task_queue/llm_response.json, but validation failed:\n\n%1\n\n"
                        "The structured diagnostic has been written to task_queue/pending_request.json "
                        "(and task_queue/validation_error.json).\n"
                        "Tell the IDE chatbot to revise task_queue/llm_response.json.")
                    .arg(errMsg));
            return;
        }

        QString generatedStagePath = stagePath;
        if (!taskId.isEmpty()) {
            const QString completedPath = QDir(projectRoot).filePath(
                QString("task_queue/completed_%1.json").arg(taskId));
            const QString completedStage = readJsonFile(completedPath).value("completed_stage").toString();
            if (!completedStage.isEmpty() && QFileInfo::exists(completedStage)) {
                generatedStagePath = completedStage;
            }
        }
        const bool generatedSceneStage = generatedStagePath.endsWith("scene.usda");
        if (isSceneStage && !generatedSceneStage && myCurrentScene >= 0 &&
            myCurrentScene < myStoryboardScenes.size()) {
            const QString sceneJsonPath = QFileInfo(stagePath).dir().filePath("scene.json");
            myStoryboardScenes[myCurrentScene].prompt =
                readJsonFile(sceneJsonPath).value("prompt").toString();
            const QString shotDirName = QFileInfo(generatedStagePath).dir().dirName();
            bool validIndex = false;
            const int shotNumber = shotDirName.mid(QStringLiteral("shot_").size()).toInt(&validIndex);
            if (shotDirName.startsWith("shot_") && validIndex && shotNumber > 0) {
                QVector<StoryboardPanel>& panels = storyboardPanels();
                if (panels.size() < shotNumber) panels.resize(shotNumber);
                StoryboardPanel& panel = panels[shotNumber - 1];
                panel.active = true;
                panel.name = QString("Shot %1").arg(shotNumber);
                panel.prompt = prompt;
                panel.usdStagePath = generatedStagePath;
                myCurrentPanel = shotNumber - 1;
                myLastEditedPanel = myCurrentPanel;
                loadShotActions(myCurrentScene, myCurrentPanel);
                updateEditorModeUi();
                if (myViewStack) myViewStack->setCurrentWidget(myEditorPage);
            }
        }

        if (myUsdViewport) {
            if (myUsdViewport->stagePath() == generatedStagePath) {
                myUsdViewport->reloadStage();
            } else {
                myUsdViewport->openStage(generatedStagePath, generatedSceneStage);
            }
            if (!generatedSceneStage) refreshGeneratedShotTimeline(generatedStagePath);
            const QImage preview = myUsdViewport->renderCurrentView();
            if (!preview.isNull()) {
                preview.save(QFileInfo(generatedStagePath).dir().filePath("thumbnail.jpg"), "JPG", 90);
                if (generatedSceneStage && myCurrentScene >= 0 && myCurrentScene < myStoryboardScenes.size()) {
                    myStoryboardScenes[myCurrentScene].render = preview;
                    myStoryboardScenes[myCurrentScene].configured = true;
                    myStoryboardScenes[myCurrentScene].usdStagePath = generatedStagePath;
                } else if (!generatedSceneStage && myCurrentScene >= 0 && myCurrentPanel >= 0 &&
                           myCurrentPanel < storyboardPanels().size()) {
                    storyboardPanels()[myCurrentPanel].render = preview;
                    storyboardPanels()[myCurrentPanel].usdStagePath = generatedStagePath;
                    if (!prompt.isEmpty()) storyboardPanels()[myCurrentPanel].prompt = prompt;
                    myLastEditedPanel = myCurrentPanel;
                }
            }
            syncLayers();
            updateSelectionPanel();
        }
        rebuildStoryboardScenes();
        rebuildStoryboardBook();
        myCanvasStatus->setText(QString("✓ PainterSpace executed LLM JSON tool calls & live-reloaded %1")
                                    .arg(QDir(projectRoot).relativeFilePath(generatedStagePath)));
    });
    process->start(pythonBin, {compileScript, QStringLiteral("--execute-llm-response"), responsePath});
}

void MainWindow::renderAndPlayUsdStageFile(const QString& stagePath, bool updateStoryboardPanel) {
    const QDir directory = QFileInfo(stagePath).dir();
    const QString renderInput = QFileInfo::exists(directory.filePath("camera_rig.json")) && QFileInfo::exists(directory.filePath("render_stage.usda")) ? directory.filePath("render_stage.usda") : stagePath;
    const QString validationError = ShotEngine::validateAuthoredUsdStage(renderInput);
    if (!validationError.isEmpty()) {
        QMessageBox::warning(this, "USD validation failed", validationError);
        return;
    }

    const ShotEngine::UsdStageInfo stageMeta = ShotEngine::inspectUsdStage(renderInput);
    const int fps = stageMeta.fps;
    const int startFrame = stageMeta.startFrame;
    const int endFrame = stageMeta.endFrame;
    const QString cameraPath = stageMeta.cameraPath;
    const QFileInfo stageInfo(stagePath);
    const QString renderDir = stageInfo.dir().filePath("frames");
    const QString pattern = QDir(renderDir).filePath("frame.###.png");
    myCanvasStatus->setText("Rendering USD frames with Hydra Storm…");
    QApplication::processEvents();
    const ShotEngine::UsdRenderResult rendered = ShotEngine::renderOpenUsdStage(
        renderInput, pattern, startFrame, endFrame, 960, cameraPath);
    if (!rendered.success) {
        QMessageBox::warning(this, "USD render failed", rendered.error);
        myCanvasStatus->setText("USD rendering failed");
        return;
    }

    const QImage firstFrameImage(rendered.frames.first());
    if (!firstFrameImage.isNull()) {
        firstFrameImage.save(stageInfo.dir().filePath("thumbnail.jpg"), "JPG", 90);
    }
    ShotEngine::encodeFrameSequence(pattern, fps, stageInfo.dir().filePath("video.mp4"));

    if (updateStoryboardPanel && myCurrentScene >= 0 && myCurrentPanel >= 0 && myCurrentPanel < storyboardPanels().size()) {
        storyboardPanels()[myCurrentPanel].prompt = myScenePrompt ? myScenePrompt->toPlainText() : QString();
        storyboardPanels()[myCurrentPanel].usdStagePath = stagePath;
        storyboardPanels()[myCurrentPanel].render = firstFrameImage;
        myLastEditedPanel = myCurrentPanel;
    }

    if (myUsdViewport) {
        myUsdViewport->setCurrentFrame(static_cast<double>(startFrame));
    }

    showPlaybackDialog(rendered.frames, fps, stageInfo.dir().dirName() + " — USD playback");
}

void MainWindow::playRenderedClipForShot(const QString& shotDirPath) {
    const QDir shotDir(shotDirPath);
    const QDir framesDir(shotDir.filePath("frames"));
    QStringList frames;
    if (framesDir.exists()) {
        const QStringList entries = framesDir.entryList(QStringList() << "frame.*.png", QDir::Files, QDir::Name);
        for (const QString& entry : entries) {
            frames.append(framesDir.filePath(entry));
        }
    }
    int fps = 24;
    const QString shotJson = shotDir.filePath("shot.json");
    if (QFileInfo::exists(shotJson)) {
        const QJsonObject meta = readJsonFile(shotJson);
        if (meta.contains("fps")) fps = meta.value("fps").toInt(24);
    }
    if (!frames.isEmpty()) {
        showPlaybackDialog(frames, fps, shotDir.dirName() + " — Playback");
        return;
    }
    const QString shotUsda = shotDir.filePath("shot.usda");
    if (QFileInfo::exists(shotUsda)) {
        renderAndPlayUsdStageFile(shotUsda, false);
    } else {
        QMessageBox::information(this, "No clip found", "No rendered frames or USD stage found for this shot.");
    }
}

void MainWindow::showPlaybackDialog(const QStringList& frames, int fps, const QString& title) {
    if (frames.isEmpty()) return;
    auto* dialog = new QDialog(this);
    dialog->setAttribute(Qt::WA_DeleteOnClose);
    dialog->setWindowTitle(title);
    dialog->resize(1000, 660);
    auto* layout = new QVBoxLayout(dialog);
    auto* frameView = new QLabel(dialog);
    frameView->setAlignment(Qt::AlignCenter);
    frameView->setMinimumSize(640, 360);
    frameView->setStyleSheet("background: #111; border: 1px solid #343e4b;");
    layout->addWidget(frameView, 1);
    auto* frameSlider = new QSlider(Qt::Horizontal, dialog);
    frameSlider->setRange(0, frames.size() - 1);
    layout->addWidget(frameSlider);
    auto* controls = new QHBoxLayout();
    auto* playPause = new QPushButton("▶ Play", dialog);
    auto* frameLabel = new QLabel(dialog);
    controls->addWidget(playPause);
    controls->addWidget(frameLabel);
    controls->addStretch();
    auto* closeButton = new QPushButton("Close", dialog);
    controls->addWidget(closeButton);
    layout->addLayout(controls);
    auto* timer = new QTimer(dialog);
    timer->setInterval(qMax(1, 1000 / fps));
    const auto showFrame = [frames, frameView, frameSlider, frameLabel](int index) {
        if (index < 0 || index >= frames.size()) return;
        const QPixmap pixmap(frames.at(index));
        frameView->setPixmap(pixmap.scaled(frameView->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
        frameLabel->setText(QString("Frame %1 / %2").arg(index + 1).arg(frames.size()));
        const QSignalBlocker blocker(frameSlider);
        frameSlider->setValue(index);
    };
    showFrame(0);
    timer->start();
    playPause->setText("Ⅱ Pause");
    connect(frameSlider, &QSlider::valueChanged, dialog, showFrame);
    connect(timer, &QTimer::timeout, dialog, [frameSlider, frames, timer, playPause, showFrame] {
        const int next = frameSlider->value() + 1;
        if (next >= frames.size()) {
            timer->stop();
            playPause->setText("▶ Play");
            return;
        }
        showFrame(next);
    });
    connect(playPause, &QPushButton::clicked, dialog, [timer, playPause, frameSlider, frames] {
        if (timer->isActive()) {
            timer->stop();
            playPause->setText("▶ Play");
        } else {
            if (frameSlider->value() >= frames.size() - 1) frameSlider->setValue(0);
            timer->start();
            playPause->setText("Ⅱ Pause");
        }
    });
    connect(closeButton, &QPushButton::clicked, dialog, &QDialog::accept);
    myCanvasStatus->setText(QString("Rendered %1 frames • ready to play").arg(frames.size()));
    dialog->exec();
}

QJsonObject MainWindow::runDirectorTool(const QStringList& arguments) const {
    if (!myUsdViewport || myUsdViewport->isSceneStage() || myUsdViewport->stagePath().isEmpty()) return {};
    const auto root = painterSpaceProjectRoot();
    const QString shot = QFileInfo(myUsdViewport->stagePath()).dir().filePath("shot.usda");
    QProcess process;
    process.setWorkingDirectory(root);
    process.start(QDir(root).filePath(".venv/bin/python"), QStringList{QDir(root).filePath("tools/shot_library/compile_shot.py"), "--out", shot} + arguments);
    if (!process.waitForFinished(10000) || process.exitCode() != 0) return {{"error", QString::fromUtf8(process.readAllStandardError())}};
    return QJsonDocument::fromJson(process.readAllStandardOutput().trimmed()).object();
}

void MainWindow::refreshDirectorState() {
    if (!myUsdViewport || myUsdViewport->isSceneStage() || !myDirectorState) return;
    if (myActionTimeline && myActionTimeline->property("catalogStage").toString() != myUsdViewport->stagePath()) {
        const auto catalog = runDirectorTool({"--brief-info"}); QStringList verbs;
        for (const auto& value : catalog["actions"].toArray()) verbs << value.toString();
        if (!verbs.isEmpty()) { myActionTimeline->setAvailableActions(verbs);myActionTimeline->setProperty("catalogStage",myUsdViewport->stagePath()); }
    }
    const QString prompt = myScenePrompt ? myScenePrompt->toPlainText().trimmed() : QString();
    const auto report = runDirectorTool({"--status", "--prompt", prompt});
    const QString state = report["status"].toString("generate");
    myDirectorState->setText(state == "generate" ? "Actors need generation" : state == "camera_only" ? "Camera changed, actors up to date" : "Actors and camera up to date");
    myRerenderCameraButton->setEnabled(state != "generate" && !myUsdViewport->cameraDocument().isEmpty());
    const auto doc = myUsdViewport->cameraDocument();
    const auto cameras = doc["cameras"].toObject();
    QSignalBlocker block(myCameraTakes); myCameraTakes->clear();
    for (auto it = cameras.begin(); it != cameras.end(); ++it) myCameraTakes->addItem(it.value().toObject()["label"].toString(it.key()), it.key());
    myCameraTakes->setCurrentIndex(myCameraTakes->findData(doc["active_camera"].toString()));
    const auto camera = cameras[doc["active_camera"].toString()].toObject();
    const auto range = camera["path"].toObject()["time_range"].toArray();
    if (range.size() == 2 && myActionTimeline) myActionTimeline->setCameraRange(range[0].toDouble(), range[1].toDouble(), camera["label"].toString("camera"));
}

void MainWindow::inspectDirectorBrief() {
    if (!myUsdViewport || myUsdViewport->isSceneStage()) return;
    saveShotActions(myCurrentScene, myCurrentPanel);
    const auto report = runDirectorTool({"--brief-info"});
    if (report.contains("error")) { myCanvasStatus->setText(report["error"].toString()); return; }
    if (myActionTimeline) {
        QStringList verbs; for (const auto& verb : report["actions"].toArray()) verbs << verb.toString();
        myActionTimeline->setAvailableActions(verbs);
    }
    myBriefLint->clear(); QStringList badPaths;
    for (const auto& value : report["lint"].toArray()) {
        auto issue = value.toObject();
        auto* item = new QListWidgetItem(issue["code"].toString() + " · " + issue["segment"].toString(issue["path"].toString()), myBriefLint);
        item->setData(Qt::UserRole, issue); item->setToolTip("Click to seek and show available fixes");
        if (issue.contains("path")) badPaths << issue["path"].toString();
    }
    myUsdViewport->setPathLint(badPaths); myBriefLint->setVisible(myBriefLint->count() > 0);
    auto* dialog = new QDialog(this); dialog->setAttribute(Qt::WA_DeleteOnClose); dialog->setWindowTitle("What the AI will see"); dialog->resize(720, 420);
    auto* layout = new QVBoxLayout(dialog); auto* digest = new QPlainTextEdit(report["digest"].toString(), dialog); digest->setReadOnly(true); layout->addWidget(digest);
    auto* close = new QDialogButtonBox(QDialogButtonBox::Close, dialog); layout->addWidget(close); connect(close, &QDialogButtonBox::rejected, dialog, &QDialog::close); dialog->show();
    refreshDirectorState();
}

void MainWindow::applyDirectorBrief(const QJsonObject& brief) {
    if (!myActionTimeline || !myUsdViewport) return;
    myActionTimeline->pushUndoState("Repair director brief");
    QJsonArray actions; const auto tracks = brief["tracks"].toObject();
    for (auto it = tracks.begin(); it != tracks.end(); ++it) {
        auto track = it.value().toObject();
        for (const auto& value : track["segments"].toArray()) {
            auto s = value.toObject(); const auto t = s["t"].toArray(); if (t.size() != 2) continue;
            s["start_s"] = t[0]; s["end_s"] = t[1];
            s["actor_path"] = track["actor_path"].toString("/World/"+it.key());
            s["actor_label"] = track["label"].toString(it.key()); actions.append(s);
        }
    }
    myActionTimeline->fromJson(actions);
    QHash<QString, QVector<QVector3D>> paths;
    const auto values = brief["paths"].toObject();
    for (auto it = values.begin(); it != values.end(); ++it) {
        const auto path = it.value().toObject(); QVector<QVector3D> points;
        for (const auto& value : path["points"].toArray()) { const auto a = value.toArray(); if (a.size() == 3) points.append(QVector3D(a[0].toDouble(), a[1].toDouble(), a[2].toDouble())); }
        paths[path["viewport_key"].toString(path["actor_path"].toString("/World/Paths/"+it.key()))] = points;
    }
    myUsdViewport->setAllPrimPaths(paths, true); myUsdViewport->setDirectorBrief(brief);
    saveShotActions(myCurrentScene, myCurrentPanel);
}

void MainWindow::renderCameraTakes(bool allTakes) {
    if (!myUsdViewport || myUsdGenerationProcess || myUsdViewport->isSceneStage()) return;
    myUsdViewport->flushCameraPreview();
    myEditorPage->setEnabled(false);
    const auto root = painterSpaceProjectRoot();
    const QDir directory = QFileInfo(myUsdViewport->stagePath()).dir();
    auto* process = new QProcess(this); myUsdGenerationProcess = process;
    myRerenderCameraButton->setEnabled(false);
    myCanvasStatus->setText("Baking camera against the current actors…");
    connect(process, QOverload<int, QProcess::ExitStatus>::of(&QProcess::finished), this, [this, process, directory, allTakes](int code, QProcess::ExitStatus exit) {

        if (code != 0 || exit != QProcess::NormalExit) { myUsdGenerationProcess = nullptr;process->deleteLater();myEditorPage->setEnabled(true);myCanvasStatus->setText(QString::fromUtf8(process->readAllStandardError())); refreshDirectorState(); return; }
        const auto result = QJsonDocument::fromJson(process->readAllStandardOutput()).object();
        const QString stage = result["stage"].toString(); const auto meta = ShotEngine::inspectUsdStage(stage);
        myBriefLint->clear();
        for (const auto& value : result["warnings"].toArray()) { auto issue = value.toObject(); auto* item = new QListWidgetItem(issue["code"].toString()+" · "+issue["camera"].toString(), myBriefLint); item->setData(Qt::UserRole, issue); }
        myBriefLint->setVisible(myBriefLint->count() > 0);
        auto* comparison = allTakes ? new QDialog(this) : nullptr;
        auto* row = comparison ? new QHBoxLayout(comparison) : nullptr;
        auto takes = result["takes"].toArray();
        for (const auto& value : takes) {
            auto take = value.toObject(); if (!allTakes && take["camera"] != result["camera"]) continue;
            const QString pattern = allTakes ? take["frames"].toString() : directory.filePath("frames/frame.###.png");
            myCanvasStatus->setText("Rendering camera take " + take["id"].toString()); QApplication::processEvents();
            const auto rendered = ShotEngine::renderOpenUsdStage(stage, pattern, meta.startFrame, meta.endFrame, 960, take["camera"].toString());
            if (!rendered.success) { myCanvasStatus->setText(rendered.error); break; }
            const QString video = allTakes ? QFileInfo(pattern).dir().filePath("video.mp4") : directory.filePath("video.mp4");
            ShotEngine::encodeFrameSequence(pattern, meta.fps, video);
            if (!allTakes) {
                QImage(rendered.frames.first()).save(directory.filePath("thumbnail.jpg"), "JPG", 90);
                showPlaybackDialog(rendered.frames, meta.fps, take["id"].toString());
            } else {
                auto* column = new QVBoxLayout(); auto* image = new QLabel(comparison);
                image->setPixmap(QPixmap(rendered.frames.first()).scaled(360, 240, Qt::KeepAspectRatio)); column->addWidget(image);
                auto* play = new QPushButton("Play " + take["id"].toString(), comparison);column->addWidget(play);
                connect(play,&QPushButton::clicked,this,[this,rendered,meta,take](){showPlaybackDialog(rendered.frames,meta.fps,take["id"].toString());});
                auto* choose = new QPushButton("Keep " + take["id"].toString(), comparison); column->addWidget(choose); row->addLayout(column);
                connect(choose, &QPushButton::clicked, this, [this, take, comparison]() {
                    auto doc = myUsdViewport->cameraDocument(); doc["active_camera"] = take["id"];
                    myUsdViewport->pushUndoSnapshot(); myUsdViewport->setCameraDocument(doc); myUsdViewport->bakeCurrentCameraRig(); comparison->accept(); refreshDirectorState();
                });
            }
        }
        myUsdGenerationProcess = nullptr;process->deleteLater();myEditorPage->setEnabled(true);
        if (comparison) { comparison->setAttribute(Qt::WA_DeleteOnClose); comparison->setWindowTitle("Compare camera takes"); comparison->show(); }
        myUsdViewport->reloadStage(); refreshDirectorState();
    });
    connect(process, &QProcess::errorOccurred, this, [this, process](QProcess::ProcessError e) {
        if (e == QProcess::FailedToStart) { myEditorPage->setEnabled(true);myUsdGenerationProcess = nullptr; process->deleteLater(); myCanvasStatus->setText("Unable to start camera baker"); refreshDirectorState(); }
    });
    process->start(QDir(root).filePath(".venv/bin/python"), {QDir(root).filePath("tools/shot_library/compile_shot.py"), "--out", directory.filePath("shot.usda"), "--camera-only", "--prompt", myScenePrompt->toPlainText().trimmed()});
}

void MainWindow::editCameraKeys() {
    if (!myUsdViewport || myUsdViewport->isSceneStage()) return;
    if (myUsdViewport->cameraDocument().isEmpty()) myUsdViewport->bakeCurrentCameraRig();
    auto document = myUsdViewport->cameraDocument(); const QString id = document["active_camera"].toString();
    auto camera = document["cameras"].toObject()[id].toObject(); if (camera.isEmpty()) return;
    QDialog dialog(this); dialog.setWindowTitle("Camera path, focus and lens keys"); dialog.resize(900, 580);
    auto* layout = new QVBoxLayout(&dialog);
    auto* controls = new QFormLayout();
    auto spin = [&](const QString& name, double value, double low, double high) { auto* box = new QDoubleSpinBox(&dialog); box->setRange(low, high);box->setDecimals(3);box->setValue(value);controls->addRow(name,box);return box; };
    const auto oldAim = camera["aim"].toObject(); const auto oldShake = camera["shake"].toObject();
    auto* smoothing = spin("Aim smoothing (seconds)",oldAim["smoothing_s"].toDouble(.35),0,10);
    auto* lead = spin("Lead room (seconds)",oldAim["lead_room_s"].toDouble(),0,5);
    auto* roll = spin("Roll (degrees)",oldAim["roll_deg"].toDouble(),-180,180);
    auto* shakeAmount = spin("Shake amount",oldShake["amount"].toDouble(),0,5);
    auto* shakeSeed = spin("Shake seed",oldShake["seed"].toDouble(7),0,1000000);
    auto* speedMps = spin("Path speed (m/s; zero uses timeline duration)",camera["path"].toObject()["speed_mps"].toDouble(),0,100);
    auto* profile = new QComboBox(&dialog);profile->addItems({"constant","ease_in_out"});profile->setCurrentText(camera["path"].toObject()["speed"].toObject()["profile"].toString("constant"));controls->addRow("Speed profile",profile);
    layout->addLayout(controls);
    auto* tabs = new QTabWidget(&dialog); layout->addWidget(tabs);
    auto makeTable = [&](const QString& title, const QStringList& headings) {
        auto* page = new QWidget(tabs); auto* box = new QVBoxLayout(page);
        auto* table = new QTableWidget(0, headings.size(), page); table->setHorizontalHeaderLabels(headings); box->addWidget(table);
        auto* row = new QHBoxLayout(); auto* add = new QPushButton("Add key", page); auto* remove = new QPushButton("Delete selected key", page);
        row->addWidget(add); row->addWidget(remove); box->addLayout(row);
        connect(add, &QPushButton::clicked, table, [table]() { table->insertRow(table->rowCount()); });
        connect(remove, &QPushButton::clicked, table, [table]() { if (table->currentRow() >= 0) table->removeRow(table->currentRow()); });
        tabs->addTab(page, title); return table;
    };
    auto fill = [](QTableWidget* table, const QStringList& values) { int r = table->rowCount(); table->insertRow(r); for (int c=0; c<values.size(); ++c) table->setItem(r,c,new QTableWidgetItem(values[c])); };
    auto* path = makeTable("Waypoints", {"Time (optional)", "X", "Height", "Z", "Dwell seconds", "Lens mm (optional)"});
    for (const auto& value : camera["path"].toObject()["waypoints"].toArray()) {
        const auto w = value.toObject(); const auto p = w["p"].toArray();
        fill(path, {w.contains("t") ? QString::number(w["t"].toDouble()) : "", QString::number(p[0].toDouble()), QString::number(p[1].toDouble()), QString::number(p[2].toDouble()), QString::number(w["dwell_s"].toDouble()), w.contains("focal_mm") ? QString::number(w["focal_mm"].toDouble()) : ""});
    }
    auto* focus = makeTable("Focus", {"Start", "End", "Aim mode", "Actor IDs (comma separated)", "Joint"});
    for (const auto& value : camera["aim"].toObject()["segments"].toArray()) {
        auto s = value.toObject(); auto t = s["t"].toArray(); QStringList ids; QString joint;
        for (const auto& target : s["targets"].toArray()) { ids << target.toObject()["actor"].toString(); joint = target.toObject()["joint"].toString(); }
        fill(focus, {QString::number(t[0].toDouble()), QString::number(t[1].toDouble()), s["mode"].toString(), ids.join(','), joint});
    }
    auto* lens = makeTable("Lens", {"Time", "Focal length mm"});
    fill(lens, {"0", QString::number(camera["lens"].toObject()["focal_mm"].toDouble(35))});
    for (const auto& v : camera["lens"].toObject()["keys"].toArray()) { auto k=v.toObject(); fill(lens,{QString::number(k["t"].toDouble()),QString::number(k["focal_mm"].toDouble())}); }
    auto* orientation = makeTable("Free orientation", {"Time", "Pitch", "Heading", "Roll"});
    for (const auto& v : camera["aim"].toObject()["segments"].toArray()) for (const auto& value : v.toObject()["keys"].toArray()) {
        auto k=value.toObject(); auto r=k["rotation_deg"].toArray(); if (r.size()==3) fill(orientation,{QString::number(k["t"].toDouble()),QString::number(r[0].toDouble()),QString::number(r[1].toDouble()),QString::number(r[2].toDouble())});
    }
    auto* speed = makeTable("Custom pacing", {"Normalized time", "Normalized distance"});
    for (const auto& value : camera["path"].toObject()["speed"].toObject()["keys"].toArray()) { auto k=value.toObject(); fill(speed,{QString::number(k["t"].toDouble()),QString::number(k["distance"].toDouble())}); }
    auto* help = new QLabel("Aim modes: look_at, along_path, lock_direction, free. Custom pacing must start at (0, 0) and end at (1, 1).", &dialog); help->setWordWrap(true); layout->addWidget(help);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, &dialog); layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, [&]() {
        auto text=[](QTableWidget* table,int r,int c) { return table->item(r,c) ? table->item(r,c)->text().trimmed() : QString(); };
        auto number=[&](QTableWidget* table,int r,int c) { bool ok=false; double value=text(table,r,c).toDouble(&ok); if (!ok || !std::isfinite(value)) throw QString("Enter a finite number in every required numeric cell"); return value; };
        try {
            QJsonArray points;
            for(int r=0;r<path->rowCount();++r) { QJsonObject w{{"p",QJsonArray{number(path,r,1),number(path,r,2),number(path,r,3)}},{"dwell_s",number(path,r,4)}};
                if(!text(path,r,0).isEmpty()) w["t"]=number(path,r,0); if(!text(path,r,5).isEmpty()) w["focal_mm"]=number(path,r,5); points.append(w); }
            if(points.isEmpty()) throw QString("Keep at least one camera waypoint");
            auto p=camera["path"].toObject();p["waypoints"]=points;
            QJsonArray pacing;for(int r=0;r<speed->rowCount();++r) pacing.append(QJsonObject{{"t",number(speed,r,0)},{"distance",number(speed,r,1)}});
            auto pace=p["speed"].toObject();pace["keys"]=pacing;pace["profile"]=profile->currentText();p["speed"]=pace;
            if(speedMps->value()>0)p["speed_mps"]=speedMps->value();else p.remove("speed_mps");camera["path"]=p;
            QJsonArray rotations;for(int r=0;r<orientation->rowCount();++r) rotations.append(QJsonObject{{"t",number(orientation,r,0)},{"rotation_deg",QJsonArray{number(orientation,r,1),number(orientation,r,2),number(orientation,r,3)}}});
            QJsonArray segments;for(int r=0;r<focus->rowCount();++r) {const auto mode=text(focus,r,2);if(!QStringList{"look_at","along_path","lock_direction","free"}.contains(mode)) throw QString("Choose a supported aim mode");
                QJsonArray targets;for(const auto& actor:text(focus,r,3).split(',',Qt::SkipEmptyParts)) {QJsonObject target{{"actor",actor.trimmed()}};if(!text(focus,r,4).isEmpty())target["joint"]=text(focus,r,4);targets.append(target);}
                QJsonObject seg{{"t",QJsonArray{number(focus,r,0),number(focus,r,1)}},{"mode",mode},{"targets",targets}};if(mode=="free")seg["keys"]=rotations;segments.append(seg);}
            auto a=camera["aim"].toObject();a["segments"]=segments;a["smoothing_s"]=smoothing->value();a["lead_room_s"]=lead->value();a["roll_deg"]=roll->value();camera["aim"]=a;
            auto shake=camera["shake"].toObject();shake["amount"]=shakeAmount->value();shake["seed"]=int(shakeSeed->value());camera["shake"]=shake;
            QJsonArray keys;for(int r=0;r<lens->rowCount();++r) keys.append(QJsonObject{{"t",number(lens,r,0)},{"focal_mm",number(lens,r,1)}});
            auto l=camera["lens"].toObject(); l["keys"]=keys; if(!keys.isEmpty())l["focal_mm"]=keys.first().toObject()["focal_mm"];camera["lens"]=l;
            auto cameras=document["cameras"].toObject();cameras[id]=camera;document["cameras"]=cameras;
            myUsdViewport->pushUndoSnapshot();myUsdViewport->setCameraDocument(document);myUsdViewport->bakeCurrentCameraRig();dialog.accept();refreshDirectorState();
        } catch(const QString& error) { QMessageBox::information(&dialog,"Camera keys",error); }
    });
    dialog.exec();
}
