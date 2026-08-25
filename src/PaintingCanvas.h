#pragma once

#include <QColor>
#include <QImage>
#include <QOpenGLBuffer>
#include <QOpenGLFunctions>
#include <QOpenGLShaderProgram>
#include <QOpenGLWidget>
#include <QMatrix4x4>
#include <QVector>
#include <QVector3D>
#include <QVector4D>

class QMouseEvent;
class QWheelEvent;
class QPainter;

enum class PaintingTool { Select, Brush, Eraser };
enum class AssetType { Human, Tree, Mountain, Rock, Light, TextBlock, DowntownTower, ResidentialNeighborhood, ResidentialIntersection, ResidentialAvenue, Ocean, GrandHarbour, FacialExpression, BaseballBat, Pistol };

enum class HumanJoint {
    Pelvis,
    Chest,
    Neck,
    Head,
    ShoulderLeft,
    ElbowLeft,
    WristLeft,
    ShoulderRight,
    ElbowRight,
    WristRight,
    HipLeft,
    KneeLeft,
    AnkleLeft,
    HipRight,
    KneeRight,
    AnkleRight,
    Count
};

struct PaintingAsset {
    int id = -1;
    QString name;
    AssetType type = AssetType::Human;
    QString text = "TEXT";
    QString fontFamily = "Arial";
    bool fontItalic = false;
    bool fontBold = false;
    bool textVertical = false;
    bool textFlat = false;
    QVector3D textBlockDimensions = QVector3D(4.4f, 1.8f, 0.38f);
    QVector3D position;
    QVector<QVector3D> joints;
    QString expression = "none";
    int attachedHumanId = -1;
    double scale = 1.0;
    bool visible = true;
};

class PaintingCanvas : public QOpenGLWidget, protected QOpenGLFunctions {
    Q_OBJECT

public:
    explicit PaintingCanvas(QWidget* parent = nullptr);

    static int humanJointCount();
    static QString humanJointName(int jointIndex);

    void setTool(PaintingTool tool);
    void setBrushColor(const QColor& color);
    void setBrushSize(double size);
    void setBrushOpacity(double opacity);
    void setBackgroundColor(const QColor& color);
    void setGroundPlaneVisible(bool visible);
    void addAsset(AssetType type, const QString& variant = QString());
    void selectAsset(int id);
    void selectHumanJoint(int jointIndex);
    void setAssetVisible(int id, bool visible);
    QVector3D selectedAssetPosition() const;
    void setSelectedAssetPosition(const QVector3D& position);
    void setSelectedAssetScale(double scale);
    void setSelectedAssetText(const QString& text);
    void setSelectedHumanExpression(const QString& expression);
    void setSelectedAssetFont(const QString& family, bool italic, bool bold);
    void setSelectedTextBlockOrientation(bool vertical, bool flat);
    QVector3D selectedTextBlockDimensions() const;
    void setSelectedTextBlockDimensions(const QVector3D& dimensions);
    QVector3D selectedHumanJointPosition(int jointIndex) const;
    void setSelectedHumanJointPosition(int jointIndex, const QVector3D& position);
    void removeSelectedAsset();
    void clearStrokes();
    void newConcept();
    QImage renderCurrentView();

    const QVector<PaintingAsset>& assets() const { return myAssets; }
    const PaintingAsset* selectedAsset() const;
    int selectedAssetId() const { return mySelectedAssetId; }
    int selectedHumanJointIndex() const { return mySelectedJointIndex; }
    QColor backgroundColor() const { return myBackgroundColor; }
    bool groundPlaneVisible() const { return myGroundPlaneVisible; }

signals:
    void selectionChanged(int assetId);
    void assetsChanged();
    void documentChanged();

protected:
    void initializeGL() override;
    void resizeGL(int width, int height) override;
    void paintGL() override;
    void mousePressEvent(QMouseEvent* event) override;
    void mouseMoveEvent(QMouseEvent* event) override;
    void mouseReleaseEvent(QMouseEvent* event) override;
    void wheelEvent(QWheelEvent* event) override;

private:
    struct Vertex {
        QVector3D position;
        QVector3D color;
        QVector3D normal = QVector3D(0.0f, 1.0f, 0.0f);
        QVector4D joints = QVector4D(0.0f, 0.0f, 0.0f, 0.0f);
        QVector4D weights = QVector4D(0.0f, 0.0f, 0.0f, 0.0f);
    };

    struct Stroke {
        QVector<QVector3D> points;
        QColor color;
        double width = 0.08;
        double opacity = 1.0;
        bool eraser = false;
    };

    QString assetName(AssetType type) const;
    QVector3D defaultAssetPosition(AssetType type) const;
    QVector<QVector3D> defaultHumanJoints() const;
    int humanJointParent(int jointIndex) const;
    bool isJointInSubtree(int rootJoint, int candidateJoint) const;
    void moveJointSubtree(PaintingAsset& asset, int jointIndex, const QVector3D& newPosition) const;
    QVector3D assetSize(const PaintingAsset& asset) const;
    QVector3D assetBoundsMin(const PaintingAsset& asset) const;
    QVector3D assetBoundsMax(const PaintingAsset& asset) const;
    int assetAt(const QPointF& screenPoint) const;
    int humanJointAt(const QPointF& screenPoint, int& jointIndex) const;
    enum class OrientationAxis { None, X, NegativeX, Y, NegativeY, Z, NegativeZ };
    QRectF orientationGizmoRect() const;
    OrientationAxis orientationAxisAt(const QPointF& screenPoint) const;
    void drawOrientationGizmo(QPainter& painter) const;
    void snapViewTo(OrientationAxis axis);
    QPointF projectToScreen(const QVector3D& worldPoint) const;
    bool screenToGround(const QPointF& screenPoint, QVector3D& groundPoint) const;
    bool screenToPlane(const QPointF& screenPoint, const QVector3D& planePoint,
                       const QVector3D& planeNormal, QVector3D& worldPoint) const;
    QMatrix4x4 viewMatrix() const;
    void drawVertices(const QVector<Vertex>& vertices, GLenum mode,
                      const QVector<QMatrix4x4>* skinMatrices = nullptr);
    void drawGround();
    void drawAsset(const PaintingAsset& asset);
    void drawHumanJointHandles(const PaintingAsset& asset);
    void drawSelection(const PaintingAsset& asset);
    void drawHuman(const PaintingAsset& asset);
    bool drawHumanExpressionHead(const PaintingAsset& asset, const QVector3D& head);
    void drawImportedHuman(const PaintingAsset& asset);
    void drawTree(const QVector3D& origin, double scale);
    void drawMountain(const QVector3D& origin, double scale);
    void drawRock(const QVector3D& origin, double scale);
    void drawLight(const QVector3D& origin, double scale);
    void drawTextBlock(const PaintingAsset& asset);
    void drawDowntownTower(const PaintingAsset& asset);
    void drawResidentialNeighborhood(const PaintingAsset& asset);
    void drawResidentialIntersection(const PaintingAsset& asset);
    void drawResidentialAvenue(const PaintingAsset& asset);
    void drawOcean(const PaintingAsset& asset);
    void drawGrandHarbour(const PaintingAsset& asset);
    void drawBaseballBat(const PaintingAsset& asset);
    void drawPistol(const PaintingAsset& asset);
    void addBox(QVector<Vertex>& vertices, const QVector3D& center, const QVector3D& size, const QVector3D& color) const;
    void addSphere(QVector<Vertex>& vertices, const QVector3D& center, const QVector3D& radii, const QVector3D& color) const;
    void addCone(QVector<Vertex>& vertices, const QVector3D& center, double radius, double height, const QVector3D& color) const;
    void addCylinder(QVector<Vertex>& vertices, const QVector3D& bottom, const QVector3D& top, double radius, const QVector3D& color) const;
    void addLine(QVector<Vertex>& vertices, const QVector3D& from, const QVector3D& to, const QVector3D& color) const;

    struct ImportedVertex {
        QVector3D position;
        QVector3D normal;
        QVector4D joints;
        QVector4D weights;
    };

    struct ImportedPrimitive {
        QString name;
        QVector<ImportedVertex> vertices;
        QVector<unsigned int> indices;
        QMatrix4x4 modelMatrix;
        bool skinned = false;
    };

    struct ImportedJoint {
        QString name;
        int parent = -1;
        int appJoint = -1;
        QMatrix4x4 restGlobal;
    };

    struct ImportedHuman {
        bool loaded = false;
        QVector<ImportedPrimitive> primitives;
        QVector<ImportedJoint> joints;
        QVector<int> skinJoints;
        QVector<QMatrix4x4> inverseBindMatrices;
        QVector3D normalizationScale = QVector3D(1.0f, 1.0f, 1.0f);
        QVector3D normalizationOffset;
    };

    struct DowntownBuilding {
        bool loaded = false;
        QVector<Vertex> vertices;
    };

    bool loadImportedHuman(const QString& filePath);
    QString findImportedHumanPath() const;
    bool loadDowntownBuilding(const QString& filePath);
    QString findDowntownBuildingPath() const;
    bool loadResidentialNeighborhood(const QString& filePath);
    QString findResidentialNeighborhoodPath() const;
    bool loadResidentialIntersection(const QString& filePath);
    QString findResidentialIntersectionPath() const;
    bool loadResidentialAvenue(const QString& filePath);
    QString findResidentialAvenuePath() const;
    bool loadOcean(const QString& filePath);
    QString findOceanPath() const;
    bool loadGrandHarbour(const QString& filePath);
    QString findGrandHarbourPath() const;
    QString findFacialExpressionPath(const QString& expression) const;
    bool loadObjGeometry(const QString& filePath, DowntownBuilding& building);
    void drawFacialExpression(const PaintingAsset& asset);
    ImportedHuman myImportedHuman;
    DowntownBuilding myDowntownBuilding;
    DowntownBuilding myResidentialNeighborhood;
    DowntownBuilding myResidentialIntersection;
    DowntownBuilding myResidentialAvenue;
    DowntownBuilding myOcean;
    DowntownBuilding myGrandHarbour;
    QVector<DowntownBuilding> myFacialExpressions;
    // The human reference is intentionally a lightweight control-point body.
    // The imported Blender figure remains on disk for compatibility, but is
    // not used by the PainterSpace human asset.
    bool myUseImportedHuman = false;

    PaintingTool myTool = PaintingTool::Select;
    QColor myBrushColor = QColor("#28323b");
    QColor myBackgroundColor = QColor("#4b5963");
    double myBrushSize = 12.0;
    double myBrushOpacity = 1.0;
    QVector<PaintingAsset> myAssets;
    QVector<Stroke> myStrokes;
    int myNextAssetId = 1;
    int mySelectedAssetId = -1;
    int mySelectedJointIndex = -1;
    int myDraggedAssetId = -1;
    int myDraggedJointIndex = -1;
    QVector3D myJointDragPlanePoint;
    QVector3D myJointDragPlaneNormal;
    QVector3D myAssetDragPlanePoint;
    QVector3D myAssetDragStartPoint;
    QVector3D myAssetDragStartPosition;
    double myAssetDragSensitivity = 0.35;
    Stroke myCurrentStroke;

    QOpenGLShaderProgram* myProgram = nullptr;
    QOpenGLBuffer myVertexBuffer;
    QMatrix4x4 myProjection;
    QPointF myLastMousePos;
    double myYaw = 0.75;
    double myPitch = 0.46;
    double myDistance = 10.5;
    QVector3D myTarget = QVector3D(0.0f, 1.95f, 0.0f);
    bool myOrientationDragging = false;
    bool myOrbiting = false;
    bool myPanning = false;
    bool myRenderMode = false;
    bool myGroundPlaneVisible = true;
};
