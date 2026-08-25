#include "MainWindow.h"
#include "PaintingCanvas.h"

#include <QDoubleSpinBox>
#include <QComboBox>
#include <QCheckBox>
#include <QFileDialog>
#include <QFrame>
#include <QFontComboBox>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QImage>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QListWidgetItem>
#include <QMessageBox>
#include <QPushButton>
#include <QScrollArea>
#include <QSlider>
#include <QSignalBlocker>
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
}

MainWindow::MainWindow(QWidget* parent) : QMainWindow(parent) {
    setWindowTitle("PainterSpace — Concept Studio");
    resize(1540, 900);
    setMinimumSize(1180, 700);

    QWidget* central = new QWidget(this);
    central->setObjectName("workspace");
    setCentralWidget(central);

    QVBoxLayout* root = new QVBoxLayout(central);
    root->setContentsMargins(18, 14, 18, 16);
    root->setSpacing(12);

    QHBoxLayout* header = new QHBoxLayout();
    header->setSpacing(12);
    QLabel* mark = new QLabel("P", this);
    mark->setObjectName("brandMark");
    mark->setAlignment(Qt::AlignCenter);
    mark->setFixedSize(34, 34);
    header->addWidget(mark);

    QVBoxLayout* titleLayout = new QVBoxLayout();
    titleLayout->setSpacing(1);
    QLabel* title = new QLabel("PainterSpace", this);
    title->setObjectName("appTitle");
    QLabel* subtitle = new QLabel("3D concept studio  /  Untitled scene", this);
    subtitle->setObjectName("mutedText");
    titleLayout->addWidget(title);
    titleLayout->addWidget(subtitle);
    header->addLayout(titleLayout);
    header->addStretch();

    myCanvasStatus = new QLabel("Orbit with right-drag  •  Zoom with scroll", this);
    myCanvasStatus->setObjectName("mutedText");
    header->addWidget(myCanvasStatus);
    QPushButton* newButton = new QPushButton("New concept", this);
    newButton->setObjectName("primaryButton");
    newButton->setCursor(Qt::PointingHandCursor);
    connect(newButton, &QPushButton::clicked, this, &MainWindow::newConcept);
    header->addWidget(newButton);
    QPushButton* renderButton = new QPushButton("Render", this);
    renderButton->setObjectName("renderButton");
    renderButton->setCursor(Qt::PointingHandCursor);
    connect(renderButton, &QPushButton::clicked, this, &MainWindow::renderScene);
    header->addWidget(renderButton);
    root->addLayout(header);

    QHBoxLayout* columns = new QHBoxLayout();
    columns->setSpacing(14);

    QWidget* leftSidebar = new QWidget(this);
    leftSidebar->setFixedWidth(220);
    QVBoxLayout* leftLayout = new QVBoxLayout(leftSidebar);
    leftLayout->setContentsMargins(0, 0, 0, 0);
    leftLayout->setSpacing(2);

    QGroupBox* toolsBox = new QGroupBox("TOOLS", leftSidebar);
    toolsBox->setStyleSheet(panelStyle());
    QVBoxLayout* toolsLayout = new QVBoxLayout(toolsBox);
    toolsLayout->setSpacing(6);
    mySelectButton = makeToolButton("↖   Select / move", "selectTool");
    myBrushButton = makeToolButton("✎   Brush", "brushTool");
    myEraserButton = makeToolButton("⌫   Eraser strokes", "eraserTool");
    mySelectButton->setCheckable(true);
    myBrushButton->setCheckable(true);
    myEraserButton->setCheckable(true);
    mySelectButton->setChecked(true);
    toolsLayout->addWidget(mySelectButton);
    toolsLayout->addWidget(myBrushButton);
    toolsLayout->addWidget(myEraserButton);


    QFormLayout* brushForm = new QFormLayout();
    brushForm->setContentsMargins(0, 10, 0, 0);
    brushForm->setSpacing(8);
    QDoubleSpinBox* brushSize = new QDoubleSpinBox(toolsBox);
    brushSize->setRange(1, 100);
    brushSize->setValue(12);
    brushSize->setSuffix(" px");
    brushForm->addRow("Size", brushSize);
    QSlider* opacity = new QSlider(Qt::Horizontal, toolsBox);
    opacity->setRange(10, 100);
    opacity->setValue(100);
    brushForm->addRow("Opacity", opacity);
    toolsLayout->addLayout(brushForm);
    leftLayout->addWidget(toolsBox);

    QGroupBox* colorBox = new QGroupBox("PALETTE", leftSidebar);
    colorBox->setStyleSheet(panelStyle());
    QVBoxLayout* colorLayout = new QVBoxLayout(colorBox);
    myCurrentColorButton = makeColorButton(QColor("#28323b"));
    myCurrentColorButton->setText("  Current color");
    myCurrentColorButton->setMinimumHeight(28);
    colorLayout->addWidget(myCurrentColorButton);
    QHBoxLayout* swatches = new QHBoxLayout();
    const QList<QColor> colors = {QColor("#28323b"), QColor("#c85c4b"), QColor("#d49345"),
                                  QColor("#e5bd55"), QColor("#679b75"), QColor("#5786ae"),
                                  QColor("#a06b91"), QColor("#f1e6d0")};
    for (const QColor& color : colors) {
        QPushButton* swatch = makeColorButton(color);
        connect(swatch, &QPushButton::clicked, this, &MainWindow::chooseColor);
        swatches->addWidget(swatch);
    }
    colorLayout->addLayout(swatches);
    leftLayout->addWidget(colorBox);

    QGroupBox* canvasBox = new QGroupBox("3D VIEW", leftSidebar);
    canvasBox->setStyleSheet(panelStyle());
    QVBoxLayout* canvasLayout = new QVBoxLayout(canvasBox);
    myBackgroundButton = makeColorButton(QColor("#4b5963"));
    myBackgroundButton->setText("  Environment color");
    myBackgroundButton->setMinimumHeight(28);
    canvasLayout->addWidget(myBackgroundButton);
    QCheckBox* groundPlaneCheck = new QCheckBox("Include ground in render", canvasBox);
    groundPlaneCheck->setChecked(true);
    groundPlaneCheck->setStyleSheet("QCheckBox { color: #f5f7f9; spacing: 7px; }");
    groundPlaneCheck->setToolTip("Include the ground plane and horizon grid in exported renders");
    canvasLayout->addWidget(groundPlaneCheck);
    QPushButton* clearButton = new QPushButton("Clear ground strokes", canvasBox);
    clearButton->setObjectName("quietButton");
    canvasLayout->addWidget(clearButton);
    leftLayout->addWidget(canvasBox);

    QGroupBox* assetsBox = new QGroupBox("REFERENCE ASSETS", leftSidebar);
    assetsBox->setStyleSheet(panelStyle());
    QVBoxLayout* assetsBoxLayout = new QVBoxLayout(assetsBox);
    assetsBoxLayout->setContentsMargins(4, 12, 4, 6);
    QScrollArea* assetScroll = new QScrollArea(assetsBox);
    assetScroll->setWidgetResizable(true);
    assetScroll->setFrameStyle(QFrame::NoFrame);
    assetScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    assetScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    assetScroll->setStyleSheet("QScrollArea { background: transparent; }");
    QWidget* assetList = new QWidget(assetScroll);
    QVBoxLayout* assetsLayout = new QVBoxLayout(assetList);
    assetsLayout->setContentsMargins(0, 0, 4, 0);
    assetsLayout->setSpacing(6);
    QPushButton* humanButton = makeAssetButton("＋  Simple human body", "humanAsset");
    QPushButton* treeButton = makeAssetButton("＋  Tree", "treeAsset");
    QPushButton* mountainButton = makeAssetButton("＋  Mountain ridge", "mountainAsset");
    QPushButton* rockButton = makeAssetButton("＋  Rock group", "rockAsset");
    QPushButton* lightButton = makeAssetButton("＋  Light source", "lightAsset");
    QPushButton* textBlockButton = makeAssetButton("＋  3D text block", "textBlockAsset");
    QPushButton* downtownTowerButton = makeAssetButton("＋  Downtown Art Deco tower", "downtownTowerAsset");
    QPushButton* residentialButton = makeAssetButton("＋  Residential neighborhood", "residentialAsset");
    QPushButton* intersectionButton = makeAssetButton("＋  Residential street junction", "intersectionAsset");
    QPushButton* avenueButton = makeAssetButton("＋  Long residential avenue", "avenueAsset");
    QPushButton* oceanButton = makeAssetButton("＋  Ocean horizon", "oceanAsset");
    QPushButton* harbourButton = makeAssetButton("＋  Grand harbour", "harbourAsset");
    assetsLayout->addWidget(humanButton);
    assetsLayout->addWidget(treeButton);
    assetsLayout->addWidget(mountainButton);
    assetsLayout->addWidget(rockButton);
    assetsLayout->addWidget(lightButton);
    assetsLayout->addWidget(textBlockButton);
    assetsLayout->addWidget(downtownTowerButton);
    assetsLayout->addWidget(residentialButton);
    assetsLayout->addWidget(intersectionButton);
    assetsLayout->addWidget(avenueButton);
    assetsLayout->addWidget(oceanButton);
    assetsLayout->addWidget(harbourButton);
    assetsLayout->addStretch();
    assetScroll->setWidget(assetList);
    assetsBoxLayout->addWidget(assetScroll);
    connect(humanButton, &QPushButton::clicked, this, &MainWindow::addHuman);
    connect(treeButton, &QPushButton::clicked, this, &MainWindow::addTree);
    connect(mountainButton, &QPushButton::clicked, this, &MainWindow::addMountain);
    connect(rockButton, &QPushButton::clicked, this, &MainWindow::addRock);
    connect(lightButton, &QPushButton::clicked, this, &MainWindow::addLight);
    connect(textBlockButton, &QPushButton::clicked, this, &MainWindow::addTextBlock);
    connect(downtownTowerButton, &QPushButton::clicked, this, &MainWindow::addDowntownTower);
    connect(residentialButton, &QPushButton::clicked, this, &MainWindow::addResidentialNeighborhood);
    connect(intersectionButton, &QPushButton::clicked, this, &MainWindow::addResidentialIntersection);
    connect(avenueButton, &QPushButton::clicked, this, &MainWindow::addResidentialAvenue);
    connect(oceanButton, &QPushButton::clicked, this, &MainWindow::addOcean);
    connect(harbourButton, &QPushButton::clicked, this, &MainWindow::addGrandHarbour);
    leftLayout->addWidget(assetsBox, 1);
    columns->addWidget(leftSidebar);

    myCanvas = new PaintingCanvas(this);
    myCanvas->setObjectName("paintingCanvas");
    columns->addWidget(myCanvas, 1);

    QWidget* rightSidebar = new QWidget(this);
    rightSidebar->setFixedWidth(238);
    QVBoxLayout* rightLayout = new QVBoxLayout(rightSidebar);
    rightLayout->setContentsMargins(0, 0, 0, 0);
    rightLayout->setSpacing(2);

    QGroupBox* layersBox = new QGroupBox("LAYERS", rightSidebar);
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
        spin->setRange(-20.0, 20.0);
        spin->setSingleStep(0.1);
        spin->setDecimals(2);
        spin->setSuffix(" m");
    }
    inspectorForm->addRow("Position X", myPositionX);
    inspectorForm->addRow("Elevation Y", myPositionY);
    inspectorForm->addRow("Position Z", myPositionZ);
    myTextEdit = new QLineEdit(inspectorBox);
    myTextEdit->setMaxLength(8);
    myTextEdit->setPlaceholderText("TEXT");
    inspectorForm->addRow("Text", myTextEdit);
    myTextFont = new QFontComboBox(inspectorBox);
    inspectorForm->addRow("Font", myTextFont);
    QWidget* textStyleWidget = new QWidget(inspectorBox);
    QHBoxLayout* textStyleLayout = new QHBoxLayout(textStyleWidget);
    textStyleLayout->setContentsMargins(0, 0, 0, 0);
    textStyleLayout->setSpacing(8);
    myTextItalic = new QCheckBox("Italic", textStyleWidget);
    myTextBold = new QCheckBox("Bold", textStyleWidget);
    textStyleLayout->addWidget(myTextItalic);
    textStyleLayout->addWidget(myTextBold);
    textStyleLayout->addStretch();
    inspectorForm->addRow("Style", textStyleWidget);
    myTextDirection = new QComboBox(inspectorBox);
    myTextDirection->addItem("Horizontal", false);
    myTextDirection->addItem("Vertical (stacked)", true);
    inspectorForm->addRow("Direction", myTextDirection);
    myTextFlat = new QCheckBox("Lay flat on ground", inspectorBox);
    inspectorForm->addRow("Orientation", myTextFlat);
    myTextLength = new QDoubleSpinBox(inspectorBox);
    myTextBreadth = new QDoubleSpinBox(inspectorBox);
    myTextWidth = new QDoubleSpinBox(inspectorBox);
    myTextLength->setRange(0.5, 12.0);
    myTextBreadth->setRange(0.5, 8.0);
    myTextWidth->setRange(0.1, 12.0);
    for (QDoubleSpinBox* spin : {myTextLength, myTextBreadth, myTextWidth}) {
        spin->setSingleStep(0.1);
        spin->setDecimals(2);
        spin->setSuffix(" m");
    }
    inspectorForm->addRow("Length", myTextLength);
    inspectorForm->addRow("Breadth", myTextBreadth);
    inspectorForm->addRow("Width", myTextWidth);
    myScaleSpin = new QDoubleSpinBox(inspectorBox);
    myScaleSpin->setRange(0.25, 100.0);
    myScaleSpin->setSingleStep(0.25);
    myScaleSpin->setValue(1.0);
    myScaleSpin->setSuffix(" ×");
    inspectorForm->addRow("Scale", myScaleSpin);
    inspectorLayout->addLayout(inspectorForm);

    myPoseBox = new QGroupBox("POSE JOINT", inspectorBox);
    QVBoxLayout* poseLayout = new QVBoxLayout(myPoseBox);
    QLabel* expressionLabel = new QLabel("Facial expression", myPoseBox);
    myExpressionCombo = new QComboBox(myPoseBox);
    myExpressionCombo->addItem("Neutral", QStringLiteral("none"));
    myExpressionCombo->addItem("Happy", QStringLiteral("happy"));
    myExpressionCombo->addItem("Sad", QStringLiteral("sad"));
    myExpressionCombo->addItem("Scared", QStringLiteral("scared"));
    myExpressionCombo->addItem("Grumpy", QStringLiteral("grumpy"));
    myExpressionCombo->addItem("Terrified", QStringLiteral("terrified"));
    myExpressionCombo->addItem("Ecstatic", QStringLiteral("ecstatic"));
    myExpressionCombo->addItem("Angry", QStringLiteral("angry"));
    myExpressionCombo->addItem("Surprised", QStringLiteral("surprised"));
    poseLayout->addWidget(expressionLabel);
    poseLayout->addWidget(myExpressionCombo);
    QLabel* heldObjectsLabel = new QLabel("Add held object", myPoseBox);
    QPushButton* addBatButton = new QPushButton("＋  Baseball bat", myPoseBox);
    QPushButton* addPistolButton = new QPushButton("＋  Pistol", myPoseBox);
    addBatButton->setObjectName("quietButton");
    addPistolButton->setObjectName("quietButton");
    poseLayout->addWidget(heldObjectsLabel);
    poseLayout->addWidget(addBatButton);
    poseLayout->addWidget(addPistolButton);
    connect(addBatButton, &QPushButton::clicked, this, [this]() {
        const int humanId = myCanvas->selectedAssetId();
        myCanvas->addAsset(AssetType::BaseballBat);
        if (humanId >= 0) myCanvas->selectAsset(humanId);
    });
    connect(addPistolButton, &QPushButton::clicked, this, [this]() {
        const int humanId = myCanvas->selectedAssetId();
        myCanvas->addAsset(AssetType::Pistol);
        if (humanId >= 0) myCanvas->selectAsset(humanId);
    });
    myJointCombo = new QComboBox(myPoseBox);
    for (int i = 0; i < PaintingCanvas::humanJointCount(); ++i) {
        if (i == static_cast<int>(HumanJoint::Neck)) continue;
        myJointCombo->addItem(PaintingCanvas::humanJointName(i), i);
    }
    poseLayout->addWidget(myJointCombo);
    QFormLayout* jointForm = new QFormLayout();
    myJointX = new QDoubleSpinBox(myPoseBox);
    myJointY = new QDoubleSpinBox(myPoseBox);
    myJointZ = new QDoubleSpinBox(myPoseBox);
    for (QDoubleSpinBox* spin : {myJointX, myJointY, myJointZ}) {
        spin->setRange(-10.0, 10.0);
        spin->setSingleStep(0.05);
        spin->setDecimals(2);
    }
    myJointX->setSuffix(" m");
    myJointY->setSuffix(" m");
    myJointZ->setSuffix(" m");
    jointForm->addRow("X", myJointX);
    jointForm->addRow("Y", myJointY);
    jointForm->addRow("Z", myJointZ);
    poseLayout->addLayout(jointForm);
    QLabel* poseHint = new QLabel("Bone lengths are locked. Neck stays fixed while the head moves.", myPoseBox);
    poseHint->setObjectName("mutedText");
    poseHint->setWordWrap(true);
    poseLayout->addWidget(poseHint);
    inspectorLayout->addWidget(myPoseBox);
    QPushButton* removeButton = new QPushButton("Remove selected asset", inspectorBox);
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

    connect(mySelectButton, &QPushButton::clicked, this, &MainWindow::selectTool);
    connect(myBrushButton, &QPushButton::clicked, this, &MainWindow::brushTool);
    connect(myEraserButton, &QPushButton::clicked, this, &MainWindow::eraserTool);
    connect(brushSize, &QDoubleSpinBox::valueChanged, myCanvas, &PaintingCanvas::setBrushSize);
    connect(opacity, &QSlider::valueChanged, this, [this](int value) { myCanvas->setBrushOpacity(value / 100.0); });
    connect(clearButton, &QPushButton::clicked, myCanvas, &PaintingCanvas::clearStrokes);
    connect(myCanvas, &PaintingCanvas::selectionChanged, this, &MainWindow::selectionChanged);
    connect(myCanvas, &PaintingCanvas::assetsChanged, this, &MainWindow::syncLayers);
    connect(myCanvas, &PaintingCanvas::assetsChanged, this, [this]() { updateSelectionPanel(); });
    connect(myLayers, &QListWidget::currentRowChanged, this, [this](int row) {
        if (row >= 0 && row < myCanvas->assets().size()) myCanvas->selectAsset(myCanvas->assets().at(row).id);
    });
    connect(myLayers, &QListWidget::itemChanged, this, [this](QListWidgetItem* item) {
        if (!item) return;
        myCanvas->setAssetVisible(item->data(Qt::UserRole).toInt(), item->checkState() == Qt::Checked);
    });
    connect(myScaleSpin, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateSelectionScale);
    connect(myTextEdit, &QLineEdit::textChanged, this, &MainWindow::updateSelectionText);
    connect(myTextFont, &QFontComboBox::currentFontChanged, this, [this](const QFont&) { updateTextFont(); });
    connect(myTextItalic, &QCheckBox::checkStateChanged, this, &MainWindow::updateTextStyle);
    connect(myTextBold, &QCheckBox::checkStateChanged, this, &MainWindow::updateTextStyle);
    connect(myTextDirection, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::updateTextOrientation);
    connect(myTextFlat, &QCheckBox::checkStateChanged, this, &MainWindow::updateTextOrientation);
    connect(myTextLength, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateTextBlockDimensions);
    connect(myTextBreadth, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateTextBlockDimensions);
    connect(myTextWidth, &QDoubleSpinBox::valueChanged, this, &MainWindow::updateTextBlockDimensions);
    connect(myPositionX, &QDoubleSpinBox::valueChanged, this, &MainWindow::positionCoordinateChanged);
    connect(myPositionY, &QDoubleSpinBox::valueChanged, this, &MainWindow::positionCoordinateChanged);
    connect(myPositionZ, &QDoubleSpinBox::valueChanged, this, &MainWindow::positionCoordinateChanged);
    connect(myJointCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::poseJointChanged);
    connect(myExpressionCombo, qOverload<int>(&QComboBox::currentIndexChanged), this, &MainWindow::humanExpressionChanged);
    connect(myJointX, &QDoubleSpinBox::valueChanged, this, &MainWindow::poseCoordinateChanged);
    connect(myJointY, &QDoubleSpinBox::valueChanged, this, &MainWindow::poseCoordinateChanged);
    connect(myJointZ, &QDoubleSpinBox::valueChanged, this, &MainWindow::poseCoordinateChanged);
    connect(removeButton, &QPushButton::clicked, this, &MainWindow::removeSelectedAsset);
    connect(myBackgroundButton, &QPushButton::clicked, this, &MainWindow::setBackgroundColor);
    connect(groundPlaneCheck, &QCheckBox::toggled, this, [this](bool visible) {
        myCanvas->setGroundPlaneVisible(visible);
        myCanvasStatus->setText(visible ? "Ground plane included in renders"
                                        : "Ground plane hidden from renders  •  abstract mode");
    });
    connect(myCanvas, &PaintingCanvas::documentChanged, this, [this]() {
        myCanvasStatus->setText("Scene updated  •  drag assets or paint on the ground plane");
    });

    setStyleSheet(
        "QMainWindow, #workspace { background: #15191e; color: #e3e8ed; }"
        "QLabel { color: #dce3e9; }"
        "#appTitle { font-size: 20px; font-weight: 700; color: #f5f7f9; }"
        "#brandMark { background: #c85c4b; color: white; border-radius: 8px; font-size: 20px; font-weight: 700; }"
        "#mutedText { color: #82909d; font-size: 11px; }"
        "QDoubleSpinBox, QLineEdit, QComboBox { background: #20262e; border: 1px solid #343e4b; border-radius: 4px; padding: 5px; }"
        "#primaryButton { background: #c85c4b; color: white; border: none; border-radius: 6px; padding: 9px 14px; font-weight: 700; }"
        "#primaryButton:hover { background: #d56b59; }"
        "#renderButton { background: #d49345; color: #211b14; border: none; border-radius: 6px; padding: 9px 18px; font-weight: 700; }"
        "#renderButton:hover { background: #e2a255; }"
        "#quietButton, #dangerButton { background: #20262e; color: #aeb9c4; border: 1px solid #343e4b; border-radius: 5px; padding: 7px; }"
        "#dangerButton { color: #e38e83; }"
        "#layersList { background: #1b2027; border: none; border-radius: 5px; padding: 4px; }"
        "#layersList::item { padding: 8px 6px; color: #bac4ce; border-radius: 4px; }"
        "#layersList::item:selected { background: #304252; color: white; }"
        "#selectionName { color: #f0c47a; font-weight: 700; padding: 3px 0 7px; }"
        "#paintingCanvas { background: #1b2027; border: 1px solid #303945; border-radius: 8px; }");

    syncLayers();
    updateSelectionPanel();
    // Start with an empty scene; assets remain available from the left rail.
    myCanvasStatus->setText("Orbit with right-drag  •  Zoom with scroll  •  Add an asset from the left rail");
}

QPushButton* MainWindow::makeToolButton(const QString& text, const QString& objectName) {
    QPushButton* button = new QPushButton(text, this);
    button->setObjectName(objectName);
    button->setStyleSheet(toolStyle());
    button->setCursor(Qt::PointingHandCursor);
    return button;
}

QPushButton* MainWindow::makeAssetButton(const QString& text, const QString& objectName) {
    QPushButton* button = makeToolButton(text, objectName);
    button->setStyleSheet(toolStyle() + "QPushButton { padding: 7px; }");
    return button;
}

QPushButton* MainWindow::makeColorButton(const QColor& color) {
    QPushButton* button = new QPushButton(this);
    button->setProperty("swatchColor", color.name());
    button->setStyleSheet(QString("QPushButton { background: %1; border: 1px solid #66717d; border-radius: 4px; }").arg(color.name()));
    button->setCursor(Qt::PointingHandCursor);
    return button;
}

void MainWindow::setActiveTool(QPushButton* button) {
    mySelectButton->setChecked(button == mySelectButton);
    myBrushButton->setChecked(button == myBrushButton);
    myEraserButton->setChecked(button == myEraserButton);
}

void MainWindow::selectTool() { setActiveTool(mySelectButton); myCanvas->setTool(PaintingTool::Select); }
void MainWindow::brushTool() { setActiveTool(myBrushButton); myCanvas->setTool(PaintingTool::Brush); }
void MainWindow::eraserTool() { setActiveTool(myEraserButton); myCanvas->setTool(PaintingTool::Eraser); }

void MainWindow::chooseColor() {
    QPushButton* button = qobject_cast<QPushButton*>(sender());
    if (!button) return;
    const QColor color(button->property("swatchColor").toString());
    myCanvas->setBrushColor(color);
    myCurrentColorButton->setStyleSheet(QString("QPushButton { background: %1; color: white; border: 1px solid #66717d; border-radius: 4px; text-align: left; padding-left: 8px; }").arg(color.name()));
}

void MainWindow::addHuman() { myCanvas->addAsset(AssetType::Human); }
void MainWindow::addTree() { myCanvas->addAsset(AssetType::Tree); }
void MainWindow::addMountain() { myCanvas->addAsset(AssetType::Mountain); }
void MainWindow::addRock() { myCanvas->addAsset(AssetType::Rock); }
void MainWindow::addLight() { myCanvas->addAsset(AssetType::Light); }
void MainWindow::addTextBlock() { myCanvas->addAsset(AssetType::TextBlock); }
void MainWindow::addDowntownTower() { myCanvas->addAsset(AssetType::DowntownTower); }
void MainWindow::addResidentialNeighborhood() { myCanvas->addAsset(AssetType::ResidentialNeighborhood); }
void MainWindow::addResidentialIntersection() { myCanvas->addAsset(AssetType::ResidentialIntersection); }
void MainWindow::addResidentialAvenue() { myCanvas->addAsset(AssetType::ResidentialAvenue); }
void MainWindow::addOcean() { myCanvas->addAsset(AssetType::Ocean); }
void MainWindow::addGrandHarbour() { myCanvas->addAsset(AssetType::GrandHarbour); }

void MainWindow::syncLayers() {
    QSignalBlocker blocker(myLayers);
    myLayers->clear();
    for (const PaintingAsset& asset : myCanvas->assets()) {
        QListWidgetItem* item = new QListWidgetItem(asset.name, myLayers);
        item->setData(Qt::UserRole, asset.id);
        item->setCheckState(asset.visible ? Qt::Checked : Qt::Unchecked);
    }
    const int selectedId = myCanvas->selectedAssetId();
    for (int i = 0; i < myCanvas->assets().size(); ++i) {
        if (myCanvas->assets().at(i).id == selectedId) { myLayers->setCurrentRow(i); break; }
    }
}

void MainWindow::selectionChanged(int) { updateSelectionPanel(); syncLayers(); }

void MainWindow::updateSelectionPanel() {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    const bool hasSelection = asset != nullptr;
    mySelectionName->setText(hasSelection ? asset->name : "No asset selected");
    myPositionX->setEnabled(hasSelection);
    myPositionY->setEnabled(hasSelection);
    myPositionZ->setEnabled(hasSelection);
    if (hasSelection) {
        const QVector3D position = myCanvas->selectedAssetPosition();
        QSignalBlocker blockX(myPositionX);
        QSignalBlocker blockY(myPositionY);
        QSignalBlocker blockZ(myPositionZ);
        myPositionX->setValue(position.x());
        myPositionY->setValue(position.y());
        myPositionZ->setValue(position.z());
    }
    const bool isTextBlock = hasSelection && asset->type == AssetType::TextBlock;
    myScaleSpin->setEnabled(hasSelection && !isTextBlock);
    if (hasSelection) {
        QSignalBlocker blocker(myScaleSpin);
        myScaleSpin->setValue(asset->scale);
    }

    myTextEdit->setEnabled(isTextBlock);
    myTextFont->setEnabled(isTextBlock);
    myTextItalic->setEnabled(isTextBlock);
    myTextBold->setEnabled(isTextBlock);
    myTextDirection->setEnabled(isTextBlock);
    myTextFlat->setEnabled(isTextBlock);
    myTextLength->setEnabled(isTextBlock);
    myTextBreadth->setEnabled(isTextBlock);
    myTextWidth->setEnabled(isTextBlock);
    if (isTextBlock) {
        QSignalBlocker blocker(myTextEdit);
        myTextEdit->setText(asset->text);
        QSignalBlocker blockFont(myTextFont);
        myTextFont->setCurrentFont(QFont(asset->fontFamily));
        QSignalBlocker blockItalic(myTextItalic);
        QSignalBlocker blockBold(myTextBold);
        myTextItalic->setChecked(asset->fontItalic);
        myTextBold->setChecked(asset->fontBold);
        QSignalBlocker blockDirection(myTextDirection);
        myTextDirection->setCurrentIndex(asset->textVertical ? 1 : 0);
        QSignalBlocker blockFlat(myTextFlat);
        myTextFlat->setChecked(asset->textFlat);
        const QVector3D dimensions = myCanvas->selectedTextBlockDimensions();
        QSignalBlocker blockLength(myTextLength);
        QSignalBlocker blockBreadth(myTextBreadth);
        QSignalBlocker blockWidth(myTextWidth);
        myTextLength->setValue(dimensions.x());
        myTextBreadth->setValue(dimensions.y());
        myTextWidth->setValue(dimensions.z());
    }

    const bool isHuman = hasSelection && asset->type == AssetType::Human;
    myPoseBox->setEnabled(isHuman);
    if (isHuman) {
        {
            QSignalBlocker blocker(myExpressionCombo);
            const int expressionIndex = myExpressionCombo->findData(asset->expression);
            myExpressionCombo->setCurrentIndex(expressionIndex >= 0 ? expressionIndex : 0);
        }
        int jointIndex = myCanvas->selectedHumanJointIndex();
        if (jointIndex < 0 || jointIndex == static_cast<int>(HumanJoint::Neck)) jointIndex = 0;
        {
            QSignalBlocker blocker(myJointCombo);
            myJointCombo->setCurrentIndex(myJointCombo->findData(jointIndex));
        }
        const QVector3D position = myCanvas->selectedHumanJointPosition(jointIndex);
        QSignalBlocker blockX(myJointX);
        QSignalBlocker blockY(myJointY);
        QSignalBlocker blockZ(myJointZ);
        myJointX->setValue(position.x());
        myJointY->setValue(position.y());
        myJointZ->setValue(position.z());
    }
}

void MainWindow::updateSelectionScale(double scale) {
    if (myCanvas->selectedAssetId() >= 0) myCanvas->setSelectedAssetScale(scale);
}

void MainWindow::updateSelectionText(const QString& text) {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    if (asset && asset->type == AssetType::TextBlock) myCanvas->setSelectedAssetText(text);
}

void MainWindow::updateTextFont() {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    if (!asset || asset->type != AssetType::TextBlock) return;
    myCanvas->setSelectedAssetFont(myTextFont->currentFont().family(), myTextItalic->isChecked(), myTextBold->isChecked());
}

void MainWindow::updateTextStyle() {
    updateTextFont();
}

void MainWindow::updateTextOrientation() {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    if (!asset || asset->type != AssetType::TextBlock) return;
    myCanvas->setSelectedTextBlockOrientation(
        myTextDirection->currentData().toBool(), myTextFlat->isChecked());
}

void MainWindow::positionCoordinateChanged(double) {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    if (!asset) return;
    QVector3D position = myCanvas->selectedAssetPosition();
    if (sender() == myPositionX) position.setX(static_cast<float>(myPositionX->value()));
    if (sender() == myPositionY) position.setY(static_cast<float>(myPositionY->value()));
    if (sender() == myPositionZ) position.setZ(static_cast<float>(myPositionZ->value()));
    myCanvas->setSelectedAssetPosition(position);
}

void MainWindow::updateTextBlockDimensions(double) {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    if (!asset || asset->type != AssetType::TextBlock) return;
    myCanvas->setSelectedTextBlockDimensions(QVector3D(
        static_cast<float>(myTextLength->value()),
        static_cast<float>(myTextBreadth->value()),
        static_cast<float>(myTextWidth->value())));
}

void MainWindow::poseJointChanged(int) {
    const int jointIndex = myJointCombo->currentData().toInt();
    myCanvas->selectHumanJoint(jointIndex);
    updateSelectionPanel();
}

void MainWindow::humanExpressionChanged(int) {
    const QString expression = myExpressionCombo->currentData().toString();
    myCanvas->setSelectedHumanExpression(expression);
}

void MainWindow::poseCoordinateChanged(double) {
    const PaintingAsset* asset = myCanvas->selectedAsset();
    const int jointIndex = myCanvas->selectedHumanJointIndex();
    if (!asset || asset->type != AssetType::Human || jointIndex < 0) return;
    QVector3D position = myCanvas->selectedHumanJointPosition(jointIndex);
    if (sender() == myJointX) position.setX(myJointX->value());
    if (sender() == myJointY) position.setY(myJointY->value());
    if (sender() == myJointZ) position.setZ(myJointZ->value());
    myCanvas->setSelectedHumanJointPosition(jointIndex, position);
}

void MainWindow::removeSelectedAsset() { myCanvas->removeSelectedAsset(); }

void MainWindow::newConcept() {
    myCanvas->newConcept();
    myCanvasStatus->setText("Fresh 3D scene  •  add a reference or paint on the ground plane");
}

void MainWindow::renderScene() {
    const QString fileName = QFileDialog::getSaveFileName(
        this, "Render current 3D view", "PainterSpace-render.png", "PNG image (*.png)");
    if (fileName.isEmpty()) return;

    const QImage renderedView = myCanvas->renderCurrentView();
    if (!renderedView.save(fileName, "PNG")) {
        QMessageBox::warning(this, "Render failed", "The rendered image could not be saved.");
        return;
    }

    myCanvasStatus->setText(QString("Rendered view saved  •  %1 × %2 px")
                                .arg(renderedView.width())
                                .arg(renderedView.height()));
}

void MainWindow::setBackgroundColor() {
    const QString current = myCanvas->backgroundColor().name();
    const QColor next = current == "#4b5963" ? QColor("#efe5d0") : QColor("#4b5963");
    myCanvas->setBackgroundColor(next);
    myBackgroundButton->setStyleSheet(QString("QPushButton { background: %1; color: #28323b; border: 1px solid #66717d; border-radius: 4px; text-align: left; padding-left: 8px; }").arg(next.name()));
}
