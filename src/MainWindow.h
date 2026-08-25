#pragma once

#include <QColor>
#include <QMainWindow>

class QLabel;
class QListWidget;
class QComboBox;
class QCheckBox;
class QDoubleSpinBox;
class QFontComboBox;
class QGroupBox;
class QLineEdit;
class QPushButton;
class PaintingCanvas;

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);

private slots:
    void selectTool();
    void brushTool();
    void eraserTool();
    void chooseColor();
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
    void syncLayers();
    void selectionChanged(int assetId);
    void updateSelectionScale(double scale);
    void updateSelectionText(const QString& text);
    void updateTextFont();
    void updateTextStyle();
    void updateTextOrientation();
    void positionCoordinateChanged(double value);
    void updateTextBlockDimensions(double value);
    void removeSelectedAsset();
    void newConcept();
    void renderScene();
    void setBackgroundColor();
    void humanExpressionChanged(int index);
    void poseJointChanged(int jointIndex);
    void poseCoordinateChanged(double value);

private:
    QPushButton* makeToolButton(const QString& text, const QString& objectName);
    QPushButton* makeAssetButton(const QString& text, const QString& objectName);
    QPushButton* makeColorButton(const QColor& color);
    void setActiveTool(QPushButton* button);
    void updateSelectionPanel();

    PaintingCanvas* myCanvas;
    QListWidget* myLayers;
    QLabel* mySelectionName;
    QLabel* myCanvasStatus;
    QDoubleSpinBox* myPositionX;
    QDoubleSpinBox* myPositionY;
    QDoubleSpinBox* myPositionZ;
    QLineEdit* myTextEdit;
    QFontComboBox* myTextFont;
    QCheckBox* myTextItalic;
    QCheckBox* myTextBold;
    QComboBox* myTextDirection;
    QCheckBox* myTextFlat;
    QDoubleSpinBox* myTextLength;
    QDoubleSpinBox* myTextBreadth;
    QDoubleSpinBox* myTextWidth;
    QDoubleSpinBox* myScaleSpin;
    QGroupBox* myPoseBox;
    QComboBox* myExpressionCombo;
    QComboBox* myJointCombo;
    QDoubleSpinBox* myJointX;
    QDoubleSpinBox* myJointY;
    QDoubleSpinBox* myJointZ;
    QPushButton* mySelectButton;
    QPushButton* myBrushButton;
    QPushButton* myEraserButton;
    QPushButton* myCurrentColorButton;
    QPushButton* myBackgroundButton;
};
