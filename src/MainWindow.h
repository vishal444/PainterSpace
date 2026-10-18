#pragma once

#include <QColor>
#include <QImage>
#include <QMainWindow>
#include <QVector>

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

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);
    UsdViewportWidget* usdViewport() const { return myUsdViewport; }

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
    void confirmSceneSetupAndOpenShots();
    void showStoryboardBook();
    void startStoryboardPanel();
    void startStoryboardPanelAt(int panelIndex);
    void openStoryboardPanel(int panelIndex);
    void deleteStoryboardPanel(int panelIndex);
    void generateUsdForCurrentPanel();
    void useUsdAssetInPrompt(const QString& assetPath);

private:
    struct StoryboardPanel;
    struct AssetEntry;

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
    QProcess* myUsdGenerationProcess = nullptr;
    QDoubleSpinBox* myPositionX;
    QDoubleSpinBox* myPositionY;
    QDoubleSpinBox* myPositionZ;
    QDoubleSpinBox* myRotationX;
    QDoubleSpinBox* myRotationY;
    QDoubleSpinBox* myRotationZ;
    QDoubleSpinBox* myScaleSpin;
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
    QPushButton* myEditorPrimaryActionButton = nullptr;
    QString myProjectTitle = QStringLiteral("Storyboard Project");
    QVector<StoryboardScene> myStoryboardScenes;
    int myCurrentScene = -1;
    int myCurrentPanel = -1;
    int myLastEditedPanel = -1;
};
