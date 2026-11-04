#pragma once

#include <QColor>
#include <QImage>
#include <QMainWindow>
#include <QVector>

#include "ActionTimelineWidget.h"
#include "AssetTypes.h"

class QLabel;
class QListWidget;
class QDoubleSpinBox;
class QGroupBox;
class QPlainTextEdit;
class QProcess;
class QPushButton;
class UsdViewportWidget;
class QGridLayout;
class QStackedWidget;
class QWidget;
class QLineEdit;
class QComboBox;
class QVBoxLayout;
class QCheckBox;
class QSlider;
class QToolButton;

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);
    UsdViewportWidget* usdViewport() const { return myUsdViewport; }

    enum class ShotMode {
        PromptLlm,
        DirectorManual,
        Hybrid
    };
    ShotMode shotMode() const { return myShotMode; }
    void setShotMode(ShotMode mode);

private slots:
    void addHuman();
    void addTree();
    void addMountain();
    void addRock();
    void addLight();
    void addTextBlock();
    void addDowntownTower();
    void addResidentialNeighborhood();
    void addResidentialIntersection();
    void addResidentialAvenue();
    void addOcean();
    void addGrandHarbour();
    void addMustang();
    void addPontiac();
    void syncLayers();
    void selectionChanged();
    void updateSelectionScale(double scale);
    void updateSelectionRotation(double angle);
    void positionCoordinateChanged(double value);
    void removeSelectedAsset();
    void newConcept();
    void renderScene();
    void showProjectExplorer();
    void createStoryboardProject();
    void openStoryboardProject();
    void addStoryboardScene();
    void showStoryboardScenes();
    void openStoryboardScene(int sceneIndex);
    void openSceneSetup(int sceneIndex);
    void saveCurrentSceneEdits();
    void confirmSceneSetupAndOpenShots();
    void showStoryboardBook();
    void startStoryboardPanel();
    void startStoryboardPanelAt(int panelIndex);
    void openStoryboardPanel(int panelIndex);
    void deleteStoryboardPanel(int panelIndex);
    void generateUsdForCurrentPanel();
    void useUsdAssetInPrompt(const QString& assetPath);
    void pollAgentLlmResponse();

private:
    struct StoryboardPanel;
    struct AssetEntry;

    void setupCameraInspector(QVBoxLayout* parentLayout);
    void updateCameraInspectorUi();

    QPushButton* makeAssetButton(const QString& text, const QString& objectName);
    void populateAssetEntries();
    void refreshAssetList();
    void filterAssetsByCategory(int index);
    void handleAssetClicked(const AssetEntry& entry);
    void updateSelectionPanel();
    void updateEditorModeUi();
    void loadProjectScenesFromDisk();
    QVector<StoryboardPanel>& storyboardPanels();
    const QVector<StoryboardPanel>& storyboardPanels() const;
    void rebuildStoryboardBook();
    void rebuildStoryboardScenes();
    void renderAndPlayUsdStageFile(const QString& stagePath, bool updateStoryboardPanel = false);
    void playRenderedClipForShot(const QString& shotDirPath);
    void showPlaybackDialog(const QStringList& frames, int fps, const QString& title);
    QString ensureSceneStageFile(int sceneIndex) const;
    QString ensureShotStageFile(int sceneIndex, int panelIndex) const;
    void toggleDrawPathForSelectedPrim();
    void clearPathForSelectedPrim();
    void updatePrimPathControls();
    void saveShotActions(int sceneIndex, int panelIndex);
    QJsonObject runDirectorTool(const QStringList& arguments) const;
    void refreshDirectorState();
    void inspectDirectorBrief();
    void applyDirectorBrief(const QJsonObject& brief);
    void renderCameraTakes(bool allTakes);
    void editCameraKeys();
    void loadShotActions(int sceneIndex, int panelIndex);
    void refreshGeneratedShotTimeline(const QString& stagePath);
    void syncTimelineMovementDistances(bool recordUndo = true);

    enum class UndoActionSource {
        Viewport,
        Timeline
    };
    QVector<UndoActionSource> myGlobalUndoStack;
    void performGlobalUndo();
    void updateEditorUndoButtonState();

    struct AssetEntry {
        QString name;
        QString category;
        QString id;
        QString path;
        QString basePrimName;
        QString forwardAxis = QStringLiteral("-Z");
        double defaultScale = 1.0;
        bool isProcedural = false;
        AssetType proceduralType = AssetType::Tree;
        bool isFacialExpression = false;
        QPushButton* button = nullptr;
    };

    struct StoryboardPanel {
        bool active = false;
        QString name;
        QImage render;
        QString prompt;
        QString usdStagePath;
        QVector<ActionSegment> actionTimeline;
        QHash<QString, QVector<QVector3D>> assetPaths;
        double durationS = 15.0;
    };

    struct StoryboardScene {
        QString name;
        QString prompt;
        QImage render;
        QString usdStagePath;
        bool configured = false;
        bool loadFromDisk = false;
        QVector<StoryboardPanel> panels;
    };

    UsdViewportWidget* myUsdViewport = nullptr;
    QComboBox* myAssetCategoryCombo = nullptr;
    QVBoxLayout* myAssetListLayout = nullptr;
    QVector<AssetEntry> myAssetEntries;
    QListWidget* myLayers;
    QLabel* mySelectionName;
    QLabel* myCanvasStatus;
    QPlainTextEdit* myScenePrompt = nullptr;
    QPushButton* myGenerateUsdButton = nullptr;
    QPushButton* myRerenderCameraButton = nullptr;
    QComboBox* myCameraTakes = nullptr;
    QComboBox* myGenerationMode = nullptr;
    bool myPickingBriefTarget = false;
    QListWidget* myBriefLint = nullptr;
    QLabel* myDirectorState = nullptr;
    QProcess* myUsdGenerationProcess = nullptr;
    QDoubleSpinBox* myPositionX;
    QDoubleSpinBox* myPositionY;
    QDoubleSpinBox* myPositionZ;
    QDoubleSpinBox* myRotationX;
    QDoubleSpinBox* myRotationY;
    QDoubleSpinBox* myRotationZ;
    QDoubleSpinBox* myScaleSpin;
    QPushButton* myRemoveAssetButton = nullptr;
    QStackedWidget* myViewStack = nullptr;
    QWidget* myProjectPage = nullptr;
    QWidget* myScenesPage = nullptr;
    QWidget* myBookPage = nullptr;
    QWidget* myEditorPage = nullptr;
    QLineEdit* myNewProjectNameEdit = nullptr;
    QLabel* myScenesTitle = nullptr;
    QGridLayout* myScenesGrid = nullptr;
    QGridLayout* myBookGrid = nullptr;
    QLabel* myBookTitle = nullptr;
    QLabel* myBookSceneSummary = nullptr;
    QLabel* myBookLastEdited = nullptr;
    QPushButton* myEditorBackButton = nullptr;
    QPushButton* myEditorEditSceneButton = nullptr;
    QLabel* myEditorTitle = nullptr;
    QLabel* myEditorModeBadge = nullptr;
    QLabel* myPromptModeLabel = nullptr;
    QLabel* myPromptHintLabel = nullptr;
    QPushButton* myEditorPlayClipButton = nullptr;
    QPushButton* myEditorSaveSceneButton = nullptr;
    QPushButton* myEditorUndoButton = nullptr;
    QPushButton* myEditorPrimaryActionButton = nullptr;
    QString myProjectTitle = QStringLiteral("Storyboard Project");
    QVector<StoryboardScene> myStoryboardScenes;
    int myCurrentScene = -1;
    int myCurrentPanel = -1;
    int myLastEditedPanel = -1;

    // Director Mode & Camera Inspector
    ShotMode myShotMode = ShotMode::DirectorManual;
    QPushButton* myCameraViewModeButton = nullptr;
    QGroupBox* myCameraInspectorBox = nullptr;
    QComboBox* myShotModeCombo = nullptr;
    QComboBox* myCameraShotSizeCombo = nullptr;
    QCheckBox* myCameraFramingModeCheck = nullptr;
    QSlider* myCameraZoomSlider = nullptr;
    QLabel* myCameraZoomValueLabel = nullptr;
    QComboBox* myCameraSensorCombo = nullptr;
    QComboBox* myCameraAspectCombo = nullptr;
    QComboBox* myCameraAimModeCombo = nullptr;
    QComboBox* myCameraAimTargetCombo = nullptr;
    QPushButton* myCenterAimButton = nullptr;
    QCheckBox* myDollyZoomCheck = nullptr;
    QPushButton* myResetCameraEditsButton = nullptr;
    QLabel* myPathJointsCountLabel = nullptr;
    QPushButton* myCameraDrawPathButton = nullptr;
    QPushButton* myExtendPathButton = nullptr;
    QPushButton* myDeleteLastJointButton = nullptr;
    QPushButton* myClearPathButton = nullptr;

    // Action Timeline System
    ActionTimelineWidget* myActionTimeline = nullptr;
    bool mySyncingTimelineMovementDistances = false;

    // Prim Movement Path Controls (inside Prim Control Panel)
    QPushButton* myPrimDrawPathButton = nullptr;
    QPushButton* myPrimClearPathButton = nullptr;
    QLabel* myPrimPathInfoLabel = nullptr;
};
