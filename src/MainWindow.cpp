#include "MainWindow.h"
#include "AssetTypes.h"
#include "ShotEngine.h"
#include "UsdViewportWidget.h"
#include <QApplication>
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
#include <QVBoxLayout>

namespace {
QString panelStyle() {
    return "QGroupBox { color: #a9b3bf; font-size: 11px; font-weight: 700; "
           "border: 1px solid #303945; border-radius: 8px; margin-top: 12px; padding: 16px 10px 10px; }"
           "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }";
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
    leftSidebar->setFixedWidth(260);
    QVBoxLayout* leftLayout = new QVBoxLayout(leftSidebar);
    leftLayout->setContentsMargins(0, 0, 0, 0);
    leftLayout->setSpacing(0);

    QGroupBox* assetsBox = new QGroupBox("ASSETS", leftSidebar);
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
    assetScroll->setStyleSheet("QScrollArea { background: transparent; }");
    QWidget* assetList = new QWidget(assetScroll);
    myAssetListLayout = new QVBoxLayout(assetList);
    myAssetListLayout->setContentsMargins(0, 4, 4, 0);
    myAssetListLayout->setSpacing(6);

    populateAssetEntries();

    assetScroll->setWidget(assetList);
    assetsBoxLayout->addWidget(assetScroll, 1);

    connect(myAssetCategoryCombo, QOverload<int>::of(&QComboBox::currentIndexChanged),
            this, &MainWindow::filterAssetsByCategory);

    leftLayout->addWidget(assetsBox, 1);
    columns->addWidget(leftSidebar);

    myUsdViewport = new UsdViewportWidget(this);
    myUsdViewport->setObjectName("usdViewport");
    columns->addWidget(myUsdViewport, 1);

    QWidget* rightSidebar = new QWidget(this);
    rightSidebar->setFixedWidth(238);
    QVBoxLayout* rightLayout = new QVBoxLayout(rightSidebar);
    rightLayout->setContentsMargins(0, 0, 0, 0);
    rightLayout->setSpacing(2);

    QGroupBox* layersBox = new QGroupBox("LAYERS / PRIM TREE", rightSidebar);
    layersBox->setStyleSheet(panelStyle());
    QVBoxLayout* layersLayout = new QVBoxLayout(layersBox);
    myLayers = new QListWidget(layersBox);
    myLayers->setObjectName("layersList");
    layersLayout->addWidget(myLayers);
    rightLayout->addWidget(layersBox, 1);

    QGroupBox* inspectorBox = new QGroupBox("INSPECTOR", rightSidebar);
    inspectorBox->setStyleSheet(
        panelStyle() +
        "QDoubleSpinBox, QLineEdit, QComboBox { min-height: 30px; padding: 5px; font-size: 12px; }"
        "QCheckBox { min-height: 28px; spacing: 7px; font-size: 12px; }"
        "QLabel { font-size: 12px; }");
    QVBoxLayout* inspectorLayout = new QVBoxLayout(inspectorBox);
    inspectorLayout->setSpacing(10);
    mySelectionName = new QLabel("No asset selected", inspectorBox);
    mySelectionName->setObjectName("selectionName");
    mySelectionName->setWordWrap(true);
    inspectorLayout->addWidget(mySelectionName);
    QFormLayout* inspectorForm = new QFormLayout();
    myPositionX = new QDoubleSpinBox(inspectorBox);
    myPositionY = new QDoubleSpinBox(inspectorBox);
    myPositionZ = new QDoubleSpinBox(inspectorBox);
    for (QDoubleSpinBox* spin : {myPositionX, myPositionY, myPositionZ}) {
        spin->setRange(-500.0, 500.0);
        spin->setSingleStep(0.1);
        spin->setDecimals(2);
        spin->setSuffix(" m");
    }
    inspectorForm->addRow("Position X", myPositionX);
    inspectorForm->addRow("Elevation Y", myPositionY);
    inspectorForm->addRow("Position Z", myPositionZ);
    myRotationX = new QDoubleSpinBox(inspectorBox);
    myRotationY = new QDoubleSpinBox(inspectorBox);
    myRotationZ = new QDoubleSpinBox(inspectorBox);
    for (QDoubleSpinBox* spin : {myRotationX, myRotationY, myRotationZ}) {
        spin->setRange(-360.0, 360.0);
        spin->setSingleStep(5.0);
        spin->setDecimals(1);
        spin->setSuffix("°");
    }
    inspectorForm->addRow("Rotate X", myRotationX);
    inspectorForm->addRow("Rotate Y", myRotationY);
    inspectorForm->addRow("Rotate Z", myRotationZ);
    myScaleSpin = new QDoubleSpinBox(inspectorBox);
    myScaleSpin->setRange(0.01, 100.0);
    myScaleSpin->setSingleStep(0.1);
    myScaleSpin->setValue(1.0);
    myScaleSpin->setSuffix(" ×");
    inspectorForm->addRow("Scale", myScaleSpin);
    inspectorLayout->addLayout(inspectorForm);

    QPushButton* removeButton = new QPushButton("Remove selected prim", inspectorBox);
    removeButton->setObjectName("dangerButton");
    inspectorLayout->addWidget(removeButton);
    QScrollArea* inspectorScroll = new QScrollArea(rightSidebar);
    inspectorScroll->setWidgetResizable(true);
    inspectorScroll->setFrameStyle(QFrame::NoFrame);
    inspectorScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    inspectorScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    inspectorScroll->setStyleSheet("QScrollArea { background: transparent; }");
    inspectorScroll->setWidget(inspectorBox);
    rightLayout->addWidget(inspectorScroll, 1);
    columns->addWidget(rightSidebar);
    root->addLayout(columns, 1);

    connect(myUsdViewport, &UsdViewportWidget::selectionChanged, this, [this](const QString&) {
        updateSelectionPanel();
        syncLayers();
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
    });
    connect(myUsdViewport, &UsdViewportWidget::statusMessageChanged, this, [this](const QString& msg) {
        if (myCanvasStatus) {
            myCanvasStatus->setText(msg);
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
    connect(removeButton, &QPushButton::clicked, this, &MainWindow::removeSelectedAsset);

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
        "#usdViewport { background: #1b2027; border: 1px solid #303945; border-radius: 8px; }");

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
    myCanvasStatus->setText("USD Hydra Storm Viewport  •  Orbit with right-drag  •  Pick & move prims in user_edits.usda");

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
        scene.panels.resize(9);
        const QString sceneFolder = QString("scenes/scene_%1").arg(index + 1, 2, 10, QChar('0'));
        const QDir sceneDir(QDir(projectRoot).filePath(sceneFolder));
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
        scene.panels.resize(9);
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
    scene.panels.resize(9);

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
    updateEditorModeUi();
    syncLayers();
    updateSelectionPanel();
    myCanvasStatus->setText(QString("Editing %1  •  Composed USD stage (%2)")
                                .arg(panel.name, QFileInfo(panel.usdStagePath).fileName()));
    if (myViewStack) myViewStack->setCurrentWidget(myEditorPage);
}

void MainWindow::deleteStoryboardPanel(int panelIndex) {
    if (myCurrentScene < 0 || panelIndex < 0 || panelIndex >= storyboardPanels().size() || !storyboardPanels().at(panelIndex).active) return;
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
            remove->setToolTip("Delete shot");
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
                    displayName = QStringLiteral("Human (Bryan)");
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
    useUsdAssetInPrompt("../../../assets/usdc_assets/humans/walking.usdc");
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
}

void MainWindow::selectionChanged() {
    updateSelectionPanel();
    syncLayers();
}

void MainWindow::updateSelectionPanel() {
    if (!myUsdViewport) return;
    const QString primPath = myUsdViewport->selectedPrimPath();
    const bool hasSelection = !primPath.isEmpty();
    mySelectionName->setText(hasSelection ? primPath : "No USD prim selected");
    myPositionX->setEnabled(hasSelection);
    myPositionY->setEnabled(hasSelection);
    myPositionZ->setEnabled(hasSelection);
    myRotationX->setEnabled(hasSelection);
    myRotationY->setEnabled(hasSelection);
    myRotationZ->setEnabled(hasSelection);
    myScaleSpin->setEnabled(hasSelection);
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
    const bool agentMode = qgetenv("OPENAI_API_KEY").isEmpty() ||
                           qEnvironmentVariable("PAINTERSPACE_LLM_PROVIDER") == "agent" ||
                           qEnvironmentVariable("PAINTERSPACE_AGENT_MODE") == "1";
    if (!agentMode && qEnvironmentVariableIsEmpty("OPENAI_API_KEY")) {
        QMessageBox::warning(this, "API key missing",
                             "Set OPENAI_API_KEY in the environment before launching PainterSpace to generate a USD scene.");
        return;
    }

    const QString prompt = myScenePrompt ? myScenePrompt->toPlainText().trimmed() : QString();
    if (prompt.isEmpty()) {
        QMessageBox::information(this, "Describe the scene or shot", "Enter a prompt before generating a USD stage.");
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
                myCanvasStatus->setText("Prompt queued in task_queue/pending_request.json • Waiting for LLM JSON in task_queue/llm_response.json…");
                if (myUsdViewport) {
                    myUsdViewport->setGenerationStatus("Waiting for IDE LLM JSON response (task_queue/llm_response.json)…", false, false);
                }
                QMessageBox::information(this, "Prompt Queued for IDE LLM Endpoint",
                    QString("Your scene prompt has been queued in task_queue/pending_request.json!\n\n"
                            "Target: %1\n"
                            "Prompt: \"%2\"\n\n"
                            "In VS Code chat, tell the assistant:\n"
                            "  \"Respond to the queued task\"\n\n"
                            "The chatbot will ONLY write the JSON tool calls to task_queue/llm_response.json, "
                            "and PainterSpace will automatically execute the function calls and compile the USD stage itself.")
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
            myCanvasStatus->setText("Prompt queued in task_queue/pending_request.json • Waiting for LLM JSON in task_queue/llm_response.json…");
            if (myUsdViewport) {
                myUsdViewport->setGenerationStatus("Waiting for IDE LLM JSON response (task_queue/llm_response.json)…", false, false);
            }
            QMessageBox::information(this, "Prompt Queued for IDE LLM Endpoint",
                QString("Your shot prompt has been queued in task_queue/pending_request.json!\n\n"
                        "Target: %1\n"
                        "Prompt: \"%2\"\n\n"
                        "In VS Code chat, tell the assistant:\n"
                        "  \"Respond to the queued task\"\n\n"
                        "The chatbot will ONLY write the JSON tool calls to task_queue/llm_response.json, "
                        "and PainterSpace will automatically execute the function calls, validate collisions, and compile the USD stage itself.")
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
    process->start(QCoreApplication::applicationFilePath(), {"--generate-usda", prompt, relativeStagePath});
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
            [this, process, stagePath, relativeStagePath, prompt, isSceneStage, projectRoot](int exitCode, QProcess::ExitStatus exitStatus) {
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

        if (myUsdViewport) {
            if (myUsdViewport->stagePath() == stagePath) {
                myUsdViewport->reloadStage();
            } else {
                myUsdViewport->openStage(stagePath, isSceneStage);
            }
            const QImage preview = myUsdViewport->renderCurrentView();
            if (!preview.isNull()) {
                preview.save(QFileInfo(stagePath).dir().filePath("thumbnail.jpg"), "JPG", 90);
                if (isSceneStage && myCurrentScene >= 0 && myCurrentScene < myStoryboardScenes.size()) {
                    myStoryboardScenes[myCurrentScene].render = preview;
                    myStoryboardScenes[myCurrentScene].configured = true;
                    myStoryboardScenes[myCurrentScene].usdStagePath = stagePath;
                } else if (!isSceneStage && myCurrentScene >= 0 && myCurrentPanel >= 0 &&
                           myCurrentPanel < storyboardPanels().size()) {
                    storyboardPanels()[myCurrentPanel].render = preview;
                    storyboardPanels()[myCurrentPanel].usdStagePath = stagePath;
                    if (!prompt.isEmpty()) storyboardPanels()[myCurrentPanel].prompt = prompt;
                    myLastEditedPanel = myCurrentPanel;
                }
            }
            syncLayers();
            updateSelectionPanel();
        }
        rebuildStoryboardScenes();
        rebuildStoryboardBook();
        myCanvasStatus->setText(QString("✓ PainterSpace executed LLM JSON tool calls & live-reloaded %1").arg(relativeStagePath));
    });
    process->start(pythonBin, {compileScript, QStringLiteral("--execute-llm-response"), responsePath});
}

void MainWindow::renderAndPlayUsdStageFile(const QString& stagePath, bool updateStoryboardPanel) {
    const QString validationError = ShotEngine::validateAuthoredUsdStage(stagePath);
    if (!validationError.isEmpty()) {
        QMessageBox::warning(this, "USD validation failed", validationError);
        return;
    }

    const ShotEngine::UsdStageInfo stageMeta = ShotEngine::inspectUsdStage(stagePath);
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
        stagePath, pattern, startFrame, endFrame, 960, cameraPath);
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
