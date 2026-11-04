#pragma once

#include <QObject>
#include <QPointF>
#include <QVector>
#include <QVector3D>

#include "CameraRig.h"

class QMouseEvent;
class QWheelEvent;
class QKeyEvent;
class UsdViewportWidget;

enum class CameraToolType {
    None,
    SelectCamera,
    Aim,
    LookAt,
    Frame,
    DrawPath,
    EditPath,
    Fly
};

class CameraToolController : public QObject {
    Q_OBJECT

public:
    explicit CameraToolController(UsdViewportWidget* viewport);
    ~CameraToolController() override;

    void setTool(CameraToolType tool);
    CameraToolType tool() const { return myTool; }

    CameraRigData& rigData() { return myRig; }
    const CameraRigData& rigData() const { return myRig; }
    void setRigData(const CameraRigData& rig);

    int selectedPointIndex() const { return mySelectedPointIndex; }
    void setSelectedPointIndex(int idx) { mySelectedPointIndex = idx; }

    double drawingPlaneHeight() const { return myDrawingPlaneHeight; }
    void setDrawingPlaneHeight(double h) { myDrawingPlaneHeight = h; }

    bool isDrawing() const { return myIsDrawing; }
    const QVector<QVector3D>& activeStroke() const { return myActiveStroke; }

    // Event handling from viewport
    bool handleMousePress(QMouseEvent* event);
    bool handleMouseMove(QMouseEvent* event);
    bool handleMouseRelease(QMouseEvent* event);
    bool handleWheel(QWheelEvent* event);
    bool handleKeyPress(QKeyEvent* event);

    void insertPointAt(int index, const QVector3D& pos);
    void removeSelectedPoint();
    void clearPath();

signals:
    void rigChanged();
    void toolChanged(CameraToolType newTool);
    void pointSelectionChanged(int pointIndex);

private:
    UsdViewportWidget* myViewport = nullptr;
    CameraToolType myTool = CameraToolType::None;
    CameraRigData myRig;

    int mySelectedPointIndex = -1;
    double myDrawingPlaneHeight = 1.6;
    bool myIsDrawing = false;
    bool myIsAiming = false;
    QPointF myLastMousePos;
    QVector<QVector3D> myActiveStroke;

    bool intersectDrawingPlane(const QPointF& screenPt, QVector3D& outWorldPt) const;
};

