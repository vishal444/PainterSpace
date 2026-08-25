#include "PaintingCanvas.h"

#include <QMouseEvent>
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QFont>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPainter>
#include <QPainterPath>
#include <QPolygonF>
#include <QQuaternion>
#include <QSurfaceFormat>
#include <QStringList>
#include <QTextStream>
#include <QVector4D>
#include <QWheelEvent>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <functional>

namespace {
constexpr float kPi = 3.14159265358979323846f;

QVector3D colorVector(const QColor& color) {
    return QVector3D(color.redF(), color.greenF(), color.blueF());
}

QVector3D mixColor(const QVector3D& first, const QVector3D& second, double amount) {
    return first * static_cast<float>(1.0 - amount) + second * static_cast<float>(amount);
}

QStringList facialExpressionNames() {
    return {QStringLiteral("happy"), QStringLiteral("sad"), QStringLiteral("scared"),
            QStringLiteral("grumpy"), QStringLiteral("terrified"), QStringLiteral("ecstatic"),
            QStringLiteral("angry"), QStringLiteral("surprised")};
}

QVector3D downtownMaterialColor(const QString& material) {
    if (material == QStringLiteral("skin")) return QVector3D(0.66f, 0.30f, 0.22f);
    if (material == QStringLiteral("skin_light")) return QVector3D(0.82f, 0.43f, 0.31f);
    if (material == QStringLiteral("skin_shadow")) return QVector3D(0.48f, 0.17f, 0.12f);
    if (material == QStringLiteral("hair")) return QVector3D(0.055f, 0.035f, 0.028f);
    if (material == QStringLiteral("eye_white")) return QVector3D(0.93f, 0.90f, 0.80f);
    if (material == QStringLiteral("eye_dark")) return QVector3D(0.018f, 0.012f, 0.011f);
    if (material == QStringLiteral("mouth_dark")) return QVector3D(0.12f, 0.012f, 0.012f);
    if (material == QStringLiteral("teeth")) return QVector3D(0.98f, 0.86f, 0.62f);
    if (material == QStringLiteral("tongue")) return QVector3D(0.78f, 0.15f, 0.16f);
    if (material == QStringLiteral("shirt")) return QVector3D(0.055f, 0.20f, 0.28f);
    if (material == QStringLiteral("window_warm")) return QVector3D(0.95f, 0.48f, 0.12f);
    if (material == QStringLiteral("window_blue") || material == QStringLiteral("glass_blue")) return QVector3D(0.035f, 0.16f, 0.22f);
    if (material == QStringLiteral("brick")) return QVector3D(0.28f, 0.095f, 0.055f);
    if (material == QStringLiteral("limestone")) return QVector3D(0.57f, 0.49f, 0.35f);
    if (material == QStringLiteral("bronze_trim")) return QVector3D(0.20f, 0.12f, 0.055f);
    if (material == QStringLiteral("mullion")) return QVector3D(0.08f, 0.075f, 0.06f);
    if (material == QStringLiteral("fire_escape")) return QVector3D(0.07f, 0.075f, 0.068f);
    if (material == QStringLiteral("water_tank")) return QVector3D(0.40f, 0.22f, 0.10f);
    if (material == QStringLiteral("water_tank_wood")) return QVector3D(0.18f, 0.075f, 0.025f);
    if (material == QStringLiteral("sign_gold")) return QVector3D(0.86f, 0.53f, 0.12f);
    if (material == QStringLiteral("asphalt")) return QVector3D(0.045f, 0.05f, 0.055f);
    if (material == QStringLiteral("sidewalk")) return QVector3D(0.38f, 0.37f, 0.34f);
    if (material == QStringLiteral("curb")) return QVector3D(0.24f, 0.24f, 0.23f);
    if (material == QStringLiteral("tree_leaf")) return QVector3D(0.035f, 0.14f, 0.055f);
    if (material == QStringLiteral("tree_trunk")) return QVector3D(0.18f, 0.07f, 0.025f);
    if (material == QStringLiteral("planter")) return QVector3D(0.12f, 0.13f, 0.12f);
    if (material == QStringLiteral("bollard")) return QVector3D(0.36f, 0.36f, 0.32f);
    if (material == QStringLiteral("antenna")) return QVector3D(0.45f, 0.46f, 0.43f);
    if (material == QStringLiteral("street_lamp")) return QVector3D(0.055f, 0.06f, 0.065f);
    if (material == QStringLiteral("metal")) return QVector3D(0.29f, 0.31f, 0.30f);
    if (material == QStringLiteral("ocean_deep")) return QVector3D(0.015f, 0.10f, 0.17f);
    if (material == QStringLiteral("ocean_mid")) return QVector3D(0.02f, 0.22f, 0.31f);
    if (material == QStringLiteral("ocean_highlight")) return QVector3D(0.10f, 0.40f, 0.46f);
    if (material == QStringLiteral("concrete")) return QVector3D(0.30f, 0.32f, 0.32f);
    if (material == QStringLiteral("crane")) return QVector3D(0.82f, 0.46f, 0.08f);
    if (material == QStringLiteral("wood")) return QVector3D(0.22f, 0.11f, 0.045f);
    if (material == QStringLiteral("container_red")) return QVector3D(0.54f, 0.10f, 0.055f);
    if (material == QStringLiteral("container_blue")) return QVector3D(0.04f, 0.20f, 0.34f);
    return QVector3D(0.10f, 0.11f, 0.115f);
}
}

PaintingCanvas::PaintingCanvas(QWidget* parent)
    : QOpenGLWidget(parent), myVertexBuffer(QOpenGLBuffer::VertexBuffer) {
    QSurfaceFormat surfaceFormat = QSurfaceFormat::defaultFormat();
    surfaceFormat.setDepthBufferSize(24);
    surfaceFormat.setStencilBufferSize(8);
    setFormat(surfaceFormat);
    setMinimumSize(600, 500);
    setMouseTracking(true);
    setFocusPolicy(Qt::StrongFocus);
    const QString importedPath = findImportedHumanPath();
    const bool importedLoaded = loadImportedHuman(importedPath);
    qInfo().noquote() << "PainterSpace human asset:" << (importedLoaded ? "loaded" : "fallback")
                      << (importedPath.isEmpty() ? QStringLiteral("(not found)") : importedPath);
    const QString downtownPath = findDowntownBuildingPath();
    const bool downtownLoaded = loadDowntownBuilding(downtownPath);
    qInfo().noquote() << "PainterSpace downtown tower:" << (downtownLoaded ? "loaded" : "not found")
                      << (downtownPath.isEmpty() ? QStringLiteral("(not found)") : downtownPath);
    const QString neighborhoodPath = findResidentialNeighborhoodPath();
    const bool neighborhoodLoaded = loadResidentialNeighborhood(neighborhoodPath);
    qInfo().noquote() << "PainterSpace residential neighborhood:" << (neighborhoodLoaded ? "loaded" : "not found")
                      << (neighborhoodPath.isEmpty() ? QStringLiteral("(not found)") : neighborhoodPath);
    const QString intersectionPath = findResidentialIntersectionPath();
    const bool intersectionLoaded = loadResidentialIntersection(intersectionPath);
    qInfo().noquote() << "PainterSpace residential intersection:" << (intersectionLoaded ? "loaded" : "not found")
                      << (intersectionPath.isEmpty() ? QStringLiteral("(not found)") : intersectionPath);
    const QString avenuePath = findResidentialAvenuePath();
    const bool avenueLoaded = loadResidentialAvenue(avenuePath);
    qInfo().noquote() << "PainterSpace residential avenue:" << (avenueLoaded ? "loaded" : "not found")
                      << (avenuePath.isEmpty() ? QStringLiteral("(not found)") : avenuePath);
    const QString oceanPath = findOceanPath();
    const bool oceanLoaded = loadOcean(oceanPath);
    qInfo().noquote() << "PainterSpace ocean:" << (oceanLoaded ? "loaded" : "not found")
                      << (oceanPath.isEmpty() ? QStringLiteral("(not found)") : oceanPath);
    const QString harbourPath = findGrandHarbourPath();
    const bool harbourLoaded = loadGrandHarbour(harbourPath);
    qInfo().noquote() << "PainterSpace grand harbour:" << (harbourLoaded ? "loaded" : "not found")
                      << (harbourPath.isEmpty() ? QStringLiteral("(not found)") : harbourPath);
    const QStringList expressionNames = facialExpressionNames();
    myFacialExpressions.resize(expressionNames.size());
    for (int index = 0; index < expressionNames.size(); ++index) {
        const QString expressionPath = findFacialExpressionPath(expressionNames.at(index));
        const bool expressionLoaded = loadObjGeometry(expressionPath, myFacialExpressions[index]);
        qInfo().noquote() << "PainterSpace facial expression " + expressionNames.at(index) + ":"
                          << (expressionLoaded ? "loaded" : "not found")
                          << (expressionPath.isEmpty() ? QStringLiteral("(not found)") : expressionPath);
    }
}

QString PaintingCanvas::findImportedHumanPath() const {
    const QString fileName = QStringLiteral("Untitled_poseable.glb");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

QString PaintingCanvas::findDowntownBuildingPath() const {
    const QString fileName = QStringLiteral("downtown_art_deco_tower.obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadDowntownBuilding(const QString& filePath) {
    return loadObjGeometry(filePath, myDowntownBuilding);
}

QString PaintingCanvas::findResidentialNeighborhoodPath() const {
    const QString fileName = QStringLiteral("residential_neighborhood.obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadResidentialNeighborhood(const QString& filePath) {
    return loadObjGeometry(filePath, myResidentialNeighborhood);
}

QString PaintingCanvas::findResidentialIntersectionPath() const {
    const QString fileName = QStringLiteral("residential_intersection.obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadResidentialIntersection(const QString& filePath) {
    return loadObjGeometry(filePath, myResidentialIntersection);
}

QString PaintingCanvas::findResidentialAvenuePath() const {
    const QString fileName = QStringLiteral("residential_avenue.obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadResidentialAvenue(const QString& filePath) {
    return loadObjGeometry(filePath, myResidentialAvenue);
}

QString PaintingCanvas::findOceanPath() const {
    const QString fileName = QStringLiteral("ocean_horizon.obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadOcean(const QString& filePath) {
    return loadObjGeometry(filePath, myOcean);
}

QString PaintingCanvas::findGrandHarbourPath() const {
    const QString fileName = QStringLiteral("grand_harbour.obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadGrandHarbour(const QString& filePath) {
    return loadObjGeometry(filePath, myGrandHarbour);
}

QString PaintingCanvas::findFacialExpressionPath(const QString& expression) const {
    const QString fileName = expression + QStringLiteral(".obj");
    const QStringList candidates = {
        QDir::current().absoluteFilePath(QStringLiteral("assets/facial_expressions/") + fileName),
        QDir(QCoreApplication::applicationDirPath()).absoluteFilePath(QStringLiteral("../assets/facial_expressions/") + fileName),
        QDir(QFileInfo(QStringLiteral(__FILE__)).absolutePath()).absoluteFilePath(QStringLiteral("../assets/facial_expressions/") + fileName)
    };
    for (const QString& candidate : candidates) {
        if (QFileInfo::exists(candidate)) return QFileInfo(candidate).canonicalFilePath();
    }
    return QString();
}

bool PaintingCanvas::loadObjGeometry(const QString& filePath, DowntownBuilding& building) {
    if (filePath.isEmpty()) return false;
    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return false;

    QTextStream stream(&file);
    QVector<QVector3D> positions;
    QVector<Vertex> vertices;
    QString material = QStringLiteral("brick");
    while (!stream.atEnd()) {
        const QString line = stream.readLine().trimmed();
        if (line.isEmpty() || line.startsWith('#')) continue;
        const QStringList parts = line.split(' ', Qt::SkipEmptyParts);
        if (parts.isEmpty()) continue;
        if (parts.at(0) == QStringLiteral("v") && parts.size() >= 4) {
            // OBJ uses Y-up here as Z-up; map it into PainterSpace's X/Y/Z axes.
            positions.push_back(QVector3D(parts.at(1).toFloat(), parts.at(3).toFloat(), parts.at(2).toFloat()));
        } else if (parts.at(0) == QStringLiteral("usemtl") && parts.size() >= 2) {
            material = parts.at(1);
        } else if (parts.at(0) == QStringLiteral("f") && parts.size() >= 4) {
            QVector<int> face;
            for (int i = 1; i < parts.size(); ++i) {
                const int slash = parts.at(i).indexOf('/');
                bool ok = false;
                const int index = parts.at(i).left(slash < 0 ? parts.at(i).size() : slash).toInt(&ok);
                if (!ok || index <= 0 || index > positions.size()) { face.clear(); break; }
                face.push_back(index - 1);
            }
            const QVector3D color = downtownMaterialColor(material);
            for (int i = 1; i + 1 < face.size(); ++i) {
                const QVector3D& a = positions.at(face.at(0));
                const QVector3D& b = positions.at(face.at(i));
                const QVector3D& c = positions.at(face.at(i + 1));
                const QVector3D normal = QVector3D::crossProduct(b - a, c - a).normalized();
                vertices.push_back({a, color, normal});
                vertices.push_back({b, color, normal});
                vertices.push_back({c, color, normal});
            }
        }
    }
    if (vertices.isEmpty()) return false;
    building.vertices = std::move(vertices);
    building.loaded = true;
    return true;
}

bool PaintingCanvas::loadImportedHuman(const QString& filePath) {
    if (filePath.isEmpty()) return false;
    QFile file(filePath);
    if (!file.open(QIODevice::ReadOnly)) return false;
    const QByteArray data = file.readAll();
    if (data.size() < 20 || std::memcmp(data.constData(), "glTF", 4) != 0) return false;

    QByteArray jsonBytes;
    QByteArray binary;
    int offset = 12;
    while (offset + 8 <= data.size()) {
        quint32 length = 0;
        quint32 type = 0;
        std::memcpy(&length, data.constData() + offset, sizeof(length));
        std::memcpy(&type, data.constData() + offset + 4, sizeof(type));
        if (offset + 8 + int(length) > data.size()) return false;
        const QByteArray chunk = data.mid(offset + 8, int(length));
        if (type == 0x4e4f534a) jsonBytes = chunk;
        if (type == 0x004e4942) binary = chunk;
        offset += 8 + int(length);
    }
    const QJsonDocument document = QJsonDocument::fromJson(jsonBytes);
    if (!document.isObject()) return false;
    const QJsonObject root = document.object();
    const QJsonArray nodes = root.value(QStringLiteral("nodes")).toArray();
    const QJsonArray meshes = root.value(QStringLiteral("meshes")).toArray();
    const QJsonArray accessors = root.value(QStringLiteral("accessors")).toArray();
    const QJsonArray bufferViews = root.value(QStringLiteral("bufferViews")).toArray();
    if (nodes.isEmpty() || meshes.isEmpty() || binary.isEmpty()) return false;

    const auto componentCount = [](const QString& type) {
        if (type == QStringLiteral("SCALAR")) return 1;
        if (type == QStringLiteral("VEC2")) return 2;
        if (type == QStringLiteral("VEC3")) return 3;
        if (type == QStringLiteral("VEC4")) return 4;
        if (type == QStringLiteral("MAT4")) return 16;
        return 0;
    };
    const auto componentSize = [](int componentType) {
        if (componentType == 5121) return 1;
        if (componentType == 5123) return 2;
        if (componentType == 5125 || componentType == 5126) return 4;
        return 0;
    };
    const auto readComponent = [](const char* address, int componentType) -> float {
        if (componentType == 5121) return float(*reinterpret_cast<const quint8*>(address));
        if (componentType == 5123) { quint16 value = 0; std::memcpy(&value, address, sizeof(value)); return float(value); }
        if (componentType == 5125) { quint32 value = 0; std::memcpy(&value, address, sizeof(value)); return float(value); }
        float value = 0.0f; std::memcpy(&value, address, sizeof(value)); return value;
    };
    const auto readAccessor = [&](int accessorIndex, int elementIndex) {
        QVector<float> values;
        if (accessorIndex < 0 || accessorIndex >= accessors.size()) return values;
        const QJsonObject accessor = accessors.at(accessorIndex).toObject();
        const QJsonObject view = bufferViews.at(accessor.value(QStringLiteral("bufferView")).toInt()).toObject();
        const int count = componentCount(accessor.value(QStringLiteral("type")).toString());
        const int componentType = accessor.value(QStringLiteral("componentType")).toInt();
        const int stride = view.value(QStringLiteral("byteStride")).toInt(count * componentSize(componentType));
        const int base = view.value(QStringLiteral("byteOffset")).toInt() + accessor.value(QStringLiteral("byteOffset")).toInt()
                         + elementIndex * stride;
        const int size = componentSize(componentType);
        if (base < 0 || base + count * size > binary.size()) return values;
        for (int component = 0; component < count; ++component)
            values.push_back(readComponent(binary.constData() + base + component * size, componentType));
        return values;
    };

    QVector<QMatrix4x4> localMatrices(nodes.size());
    QVector<QMatrix4x4> globalMatrices(nodes.size());
    QVector<int> parents(nodes.size(), -1);
    QVector<QVector<int>> children(nodes.size());
    const auto nodeMatrix = [](const QJsonObject& node) {
        QMatrix4x4 matrix;
        matrix.setToIdentity();
        const QJsonArray sourceMatrix = node.value(QStringLiteral("matrix")).toArray();
        if (sourceMatrix.size() == 16) {
            for (int column = 0; column < 4; ++column)
                for (int row = 0; row < 4; ++row)
                    matrix(row, column) = float(sourceMatrix.at(column * 4 + row).toDouble());
            return matrix;
        }
        const QJsonArray translation = node.value(QStringLiteral("translation")).toArray();
        const QJsonArray rotation = node.value(QStringLiteral("rotation")).toArray();
        const QJsonArray scale = node.value(QStringLiteral("scale")).toArray();
        if (translation.size() == 3) matrix.translate(float(translation.at(0).toDouble()), float(translation.at(1).toDouble()), float(translation.at(2).toDouble()));
        if (rotation.size() == 4) matrix.rotate(QQuaternion(float(rotation.at(3).toDouble()), float(rotation.at(0).toDouble()),
                                                              float(rotation.at(1).toDouble()), float(rotation.at(2).toDouble())));
        if (scale.size() == 3) matrix.scale(float(scale.at(0).toDouble()), float(scale.at(1).toDouble()), float(scale.at(2).toDouble()));
        return matrix;
    };
    for (int index = 0; index < nodes.size(); ++index) {
        const QJsonObject node = nodes.at(index).toObject();
        localMatrices[index] = nodeMatrix(node);
        const QJsonArray childArray = node.value(QStringLiteral("children")).toArray();
        for (const QJsonValue& childValue : childArray) {
            const int child = childValue.toInt(-1);
            if (child >= 0 && child < nodes.size()) { children[index].push_back(child); parents[child] = index; }
        }
    }
    std::function<void(int, const QMatrix4x4&)> resolveGlobal = [&](int index, const QMatrix4x4& parentMatrix) {
        globalMatrices[index] = parentMatrix * localMatrices[index];
        for (int child : children[index]) resolveGlobal(child, globalMatrices[index]);
    };
    for (int index = 0; index < nodes.size(); ++index) if (parents[index] < 0) {
        QMatrix4x4 identity; identity.setToIdentity(); resolveGlobal(index, identity);
    }

    // The Blender glTF exporter already converts Blender's Z-up scene to the
    // glTF Y-up coordinate system. PainterSpace also uses Y-up, so keep the
    // exported mesh and rig in that coordinate space. Applying another axis
    // conversion here would turn the upright character sideways and put the
    // visible joint handles out of alignment with the mesh.
    QMatrix4x4 sourceToCanvas;
    sourceToCanvas.setToIdentity();

    ImportedHuman imported;
    imported.joints.resize(nodes.size());
    const auto appJointForName = [](const QString& name) {
        if (name == "pelvis") return int(HumanJoint::Pelvis);
        if (name == "chest") return int(HumanJoint::Chest);
        if (name == "neck") return int(HumanJoint::Neck);
        if (name == "head") return int(HumanJoint::Head);
        if (name == "upper_arm.L") return int(HumanJoint::ShoulderLeft);
        if (name == "forearm.L") return int(HumanJoint::ElbowLeft);
        if (name == "hand.L") return int(HumanJoint::WristLeft);
        if (name == "upper_arm.R") return int(HumanJoint::ShoulderRight);
        if (name == "forearm.R") return int(HumanJoint::ElbowRight);
        if (name == "hand.R") return int(HumanJoint::WristRight);
        if (name == "thigh.L") return int(HumanJoint::HipLeft);
        if (name == "shin.L") return int(HumanJoint::KneeLeft);
        if (name == "foot.L") return int(HumanJoint::AnkleLeft);
        if (name == "thigh.R") return int(HumanJoint::HipRight);
        if (name == "shin.R") return int(HumanJoint::KneeRight);
        if (name == "foot.R") return int(HumanJoint::AnkleRight);
        return -1;
    };
    for (int index = 0; index < nodes.size(); ++index) {
        const QString name = nodes.at(index).toObject().value(QStringLiteral("name")).toString();
        imported.joints[index] = {name, parents[index], appJointForName(name), sourceToCanvas * globalMatrices[index]};
    }
    const QJsonArray skins = root.value(QStringLiteral("skins")).toArray();
    if (!skins.isEmpty()) {
        const QJsonObject skin = skins.at(0).toObject();
        const QJsonArray jointArray = skin.value(QStringLiteral("joints")).toArray();
        for (const QJsonValue& value : jointArray) imported.skinJoints.push_back(value.toInt());
        const int inverseBindAccessor = skin.value(QStringLiteral("inverseBindMatrices")).toInt(-1);
        if (inverseBindAccessor >= 0) {
            const QJsonObject accessor = accessors.at(inverseBindAccessor).toObject();
            const int matrixCount = accessor.value(QStringLiteral("count")).toInt();
            imported.inverseBindMatrices.reserve(matrixCount);
            for (int matrixIndex = 0; matrixIndex < matrixCount; ++matrixIndex) {
                const QVector<float> values = readAccessor(inverseBindAccessor, matrixIndex);
                QMatrix4x4 matrix;
                matrix.setToIdentity();
                if (values.size() == 16) {
                    for (int column = 0; column < 4; ++column)
                        for (int row = 0; row < 4; ++row)
                            matrix(row, column) = values.at(column * 4 + row);
                }
                imported.inverseBindMatrices.push_back(matrix);
            }
        }
    }
    if (imported.skinJoints.isEmpty()) return false;

    QVector3D rawMin(1e9f, 1e9f, 1e9f), rawMax(-1e9f, -1e9f, -1e9f);
    for (int nodeIndex = 0; nodeIndex < nodes.size(); ++nodeIndex) {
        const QJsonObject node = nodes.at(nodeIndex).toObject();
        const int meshIndex = node.value(QStringLiteral("mesh")).toInt(-1);
        if (meshIndex < 0 || meshIndex >= meshes.size()) continue;
        const QString meshName = meshes.at(meshIndex).toObject().value(QStringLiteral("name")).toString();
        if (meshName == "Plane" || node.value(QStringLiteral("name")).toString() == "Backdrop") continue;
        const QJsonArray primitiveArray = meshes.at(meshIndex).toObject().value(QStringLiteral("primitives")).toArray();
        for (const QJsonValue& primitiveValue : primitiveArray) {
            const QJsonObject primitiveObject = primitiveValue.toObject();
            const QJsonObject attributes = primitiveObject.value(QStringLiteral("attributes")).toObject();
            const int positionAccessor = attributes.value(QStringLiteral("POSITION")).toInt(-1);
            const int normalAccessor = attributes.value(QStringLiteral("NORMAL")).toInt(-1);
            if (positionAccessor < 0) continue;
            ImportedPrimitive primitive;
            primitive.name = meshName;
            primitive.modelMatrix = sourceToCanvas * globalMatrices[nodeIndex];
            primitive.skinned = node.value(QStringLiteral("skin")).toInt(-1) >= 0 && attributes.contains(QStringLiteral("JOINTS_0"));
            const int vertexCount = accessors.at(positionAccessor).toObject().value(QStringLiteral("count")).toInt();
            const int jointsAccessor = attributes.value(QStringLiteral("JOINTS_0")).toInt(-1);
            const int weightsAccessor = attributes.value(QStringLiteral("WEIGHTS_0")).toInt(-1);
            primitive.vertices.reserve(vertexCount);
            for (int vertexIndex = 0; vertexIndex < vertexCount; ++vertexIndex) {
                const QVector<float> position = readAccessor(positionAccessor, vertexIndex);
                const QVector<float> normal = readAccessor(normalAccessor, vertexIndex);
                const QVector<float> joints = readAccessor(jointsAccessor, vertexIndex);
                const QVector<float> weights = readAccessor(weightsAccessor, vertexIndex);
                if (position.size() < 3) continue;
                primitive.vertices.push_back({QVector3D(position[0], position[1], position[2]),
                                              normal.size() >= 3 ? QVector3D(normal[0], normal[1], normal[2]) : QVector3D(0, 1, 0),
                                              joints.size() >= 4 ? QVector4D(joints[0], joints[1], joints[2], joints[3]) : QVector4D(),
                                              weights.size() >= 4 ? QVector4D(weights[0], weights[1], weights[2], weights[3]) : QVector4D()});
                const QVector3D worldPoint = primitive.modelMatrix.map(primitive.vertices.back().position);
                rawMin.setX(std::min(rawMin.x(), worldPoint.x())); rawMin.setY(std::min(rawMin.y(), worldPoint.y())); rawMin.setZ(std::min(rawMin.z(), worldPoint.z()));
                rawMax.setX(std::max(rawMax.x(), worldPoint.x())); rawMax.setY(std::max(rawMax.y(), worldPoint.y())); rawMax.setZ(std::max(rawMax.z(), worldPoint.z()));
            }
            const int indexAccessor = primitiveObject.value(QStringLiteral("indices")).toInt(-1);
            if (indexAccessor >= 0) {
                const int indexCount = accessors.at(indexAccessor).toObject().value(QStringLiteral("count")).toInt();
                for (int index = 0; index < indexCount; ++index) {
                    const QVector<float> value = readAccessor(indexAccessor, index);
                    if (!value.isEmpty()) primitive.indices.push_back(unsigned(value[0]));
                }
            } else {
                for (unsigned index = 0; index < unsigned(primitive.vertices.size()); ++index) primitive.indices.push_back(index);
            }
            imported.primitives.push_back(std::move(primitive));
        }
    }
    if (imported.primitives.isEmpty()) return false;
    const float uniformScale = 4.25f / std::max(0.001f, rawMax.y() - rawMin.y());
    imported.normalizationScale = QVector3D(uniformScale, uniformScale, uniformScale);
    imported.normalizationOffset = QVector3D(-(rawMin.x() + rawMax.x()) * 0.5f * uniformScale,
                                              -rawMin.y() * uniformScale,
                                              -(rawMin.z() + rawMax.z()) * 0.5f * uniformScale);
    QMatrix4x4 normalize;
    normalize.setToIdentity();
    normalize.translate(imported.normalizationOffset);
    normalize.scale(imported.normalizationScale);
    for (ImportedJoint& joint : imported.joints) joint.restGlobal = normalize * joint.restGlobal;
    imported.loaded = true;
    myImportedHuman = imported;
    return true;
}

int PaintingCanvas::humanJointCount() { return static_cast<int>(HumanJoint::Count); }

QString PaintingCanvas::humanJointName(int jointIndex) {
    switch (static_cast<HumanJoint>(jointIndex)) {
    case HumanJoint::Pelvis: return "Hip center (pelvis)";
    case HumanJoint::Chest: return "Chest";
    case HumanJoint::Neck: return "Neck";
    case HumanJoint::Head: return "Head";
    case HumanJoint::ShoulderLeft: return "Left shoulder";
    case HumanJoint::ElbowLeft: return "Left elbow";
    case HumanJoint::WristLeft: return "Left wrist";
    case HumanJoint::ShoulderRight: return "Right shoulder";
    case HumanJoint::ElbowRight: return "Right elbow";
    case HumanJoint::WristRight: return "Right wrist";
    case HumanJoint::HipLeft: return "Left hip";
    case HumanJoint::KneeLeft: return "Left knee";
    case HumanJoint::AnkleLeft: return "Left ankle";
    case HumanJoint::HipRight: return "Right hip";
    case HumanJoint::KneeRight: return "Right knee";
    case HumanJoint::AnkleRight: return "Right ankle";
    case HumanJoint::Count: break;
    }
    return "Joint";
}

void PaintingCanvas::setTool(PaintingTool tool) { myTool = tool; update(); }
void PaintingCanvas::setBrushColor(const QColor& color) { myBrushColor = color; }
void PaintingCanvas::setBrushSize(double size) { myBrushSize = size; }
void PaintingCanvas::setBrushOpacity(double opacity) { myBrushOpacity = opacity; }
void PaintingCanvas::setBackgroundColor(const QColor& color) {
    myBackgroundColor = color;
    update();
    emit documentChanged();
}

void PaintingCanvas::setGroundPlaneVisible(bool visible) {
    if (myGroundPlaneVisible == visible) return;
    myGroundPlaneVisible = visible;
    update();
    emit documentChanged();
}

QString PaintingCanvas::assetName(AssetType type) const {
    switch (type) {
    case AssetType::Human: return "Simple human body";
    case AssetType::Tree: return "Tree";
    case AssetType::Mountain: return "Mountain ridge";
    case AssetType::Rock: return "Rock group";
    case AssetType::Light: return "Light source";
    case AssetType::TextBlock: return "3D text block";
    case AssetType::DowntownTower: return "Downtown Art Deco tower";
    case AssetType::ResidentialNeighborhood: return "Residential neighborhood";
    case AssetType::ResidentialIntersection: return "Residential street intersection";
    case AssetType::ResidentialAvenue: return "Long residential avenue";
    case AssetType::Ocean: return "Ocean horizon";
    case AssetType::GrandHarbour: return "Grand harbour";
    case AssetType::FacialExpression: return "Facial expression";
    case AssetType::BaseballBat: return "Baseball bat";
    case AssetType::Pistol: return "Pistol";
    }
    return "Reference asset";
}

QVector3D PaintingCanvas::defaultAssetPosition(AssetType type) const {
    const int count = myAssets.size();
    switch (type) {
    case AssetType::Human: return QVector3D(-1.0f + (count % 3) * 0.8f, 0.0f, 0.3f);
    // The camera orbits from the positive X/Z side, so place trees farther
    // back on Z to keep them behind foreground figures in the starter scene.
    case AssetType::Tree: return QVector3D(3.0f + (count % 2) * 1.0f, 0.0f, -4.0f);
    case AssetType::Mountain: return QVector3D(-4.2f, 0.0f, -2.2f);
    case AssetType::Rock: return QVector3D(-2.0f + (count % 2) * 1.1f, 0.0f, 1.4f);
    case AssetType::Light: return QVector3D(2.5f, 0.0f, 1.8f);
    case AssetType::TextBlock: return QVector3D(0.0f, 0.0f, -1.0f);
    case AssetType::DowntownTower: return QVector3D(0.0f, 0.0f, -2.4f);
    case AssetType::ResidentialNeighborhood: return QVector3D(0.0f, 0.0f, -2.0f);
    case AssetType::ResidentialIntersection: return QVector3D(0.0f, 0.0f, -2.0f);
    case AssetType::ResidentialAvenue: return QVector3D(0.0f, 0.0f, -2.0f);
    case AssetType::Ocean: return QVector3D(0.0f, -0.02f, 0.0f);
    case AssetType::GrandHarbour: return QVector3D(0.0f, 0.0f, 0.0f);
    case AssetType::FacialExpression: return QVector3D(-1.2f + (count % 3) * 1.2f, 0.0f, 0.7f);
    case AssetType::BaseballBat: return QVector3D(0.0f, 1.9f, -0.7f);
    case AssetType::Pistol: return QVector3D(0.8f, 2.4f, -0.8f);
    }
    return QVector3D(0.0f, 0.0f, 0.0f);
}

QVector<QVector3D> PaintingCanvas::defaultHumanJoints() const {
    QVector<QVector3D> joints = {
        QVector3D(0.0f, 1.88f, 0.0f),
        QVector3D(0.0f, 2.76f, 0.0f),
        QVector3D(0.0f, 3.42f, 0.0f),
        QVector3D(0.0f, 3.82f, 0.0f),
        QVector3D(-0.58f, 3.18f, 0.0f),
        QVector3D(-0.98f, 2.62f, 0.0f),
        QVector3D(-1.14f, 2.00f, 0.0f),
        QVector3D(0.58f, 3.18f, 0.0f),
        QVector3D(0.98f, 2.62f, 0.0f),
        QVector3D(1.14f, 2.00f, 0.0f),
        QVector3D(-0.40f, 1.88f, 0.0f),
        QVector3D(-0.30f, 0.92f, 0.0f),
        QVector3D(-0.3f, 0.0f, 0.0f),
        QVector3D(0.40f, 1.88f, 0.0f),
        QVector3D(0.30f, 0.92f, 0.0f),
        QVector3D(0.3f, 0.0f, 0.0f)
    };
    return joints;
}

int PaintingCanvas::humanJointParent(int jointIndex) const {
    switch (static_cast<HumanJoint>(jointIndex)) {
    case HumanJoint::Pelvis: return -1;
    case HumanJoint::Chest: return static_cast<int>(HumanJoint::Pelvis);
    case HumanJoint::Neck: return static_cast<int>(HumanJoint::Chest);
    case HumanJoint::Head: return static_cast<int>(HumanJoint::Neck);
    case HumanJoint::ShoulderLeft: return static_cast<int>(HumanJoint::Chest);
    case HumanJoint::ElbowLeft: return static_cast<int>(HumanJoint::ShoulderLeft);
    case HumanJoint::WristLeft: return static_cast<int>(HumanJoint::ElbowLeft);
    case HumanJoint::ShoulderRight: return static_cast<int>(HumanJoint::Chest);
    case HumanJoint::ElbowRight: return static_cast<int>(HumanJoint::ShoulderRight);
    case HumanJoint::WristRight: return static_cast<int>(HumanJoint::ElbowRight);
    case HumanJoint::HipLeft: return static_cast<int>(HumanJoint::Pelvis);
    case HumanJoint::KneeLeft: return static_cast<int>(HumanJoint::HipLeft);
    case HumanJoint::AnkleLeft: return static_cast<int>(HumanJoint::KneeLeft);
    case HumanJoint::HipRight: return static_cast<int>(HumanJoint::Pelvis);
    case HumanJoint::KneeRight: return static_cast<int>(HumanJoint::HipRight);
    case HumanJoint::AnkleRight: return static_cast<int>(HumanJoint::KneeRight);
    case HumanJoint::Count: return -1;
    }
    return -1;
}

bool PaintingCanvas::isJointInSubtree(int rootJoint, int candidateJoint) const {
    int current = candidateJoint;
    while (current >= 0) {
        if (current == rootJoint) return true;
        current = humanJointParent(current);
    }
    return false;
}

void PaintingCanvas::moveJointSubtree(PaintingAsset& asset, int jointIndex, const QVector3D& newPosition) const {
    if (asset.type != AssetType::Human || asset.joints.size() != humanJointCount()) return;
    if (jointIndex < 0 || jointIndex >= asset.joints.size()) return;

    QVector3D constrainedPosition = newPosition;
    const int parentIndex = humanJointParent(jointIndex);
    if (parentIndex >= 0) {
        const QVector3D parentPosition = asset.joints.at(parentIndex);
        const QVector3D originalOffset = asset.joints.at(jointIndex) - parentPosition;
        const float boneLength = originalOffset.length();
        QVector3D requestedOffset = newPosition - parentPosition;
        if (requestedOffset.lengthSquared() < 0.0001f) requestedOffset = originalOffset;
        if (requestedOffset.lengthSquared() >= 0.0001f) {
            constrainedPosition = parentPosition + requestedOffset.normalized() * boneLength;
        }
    }
    QVector3D delta = constrainedPosition - asset.joints.at(jointIndex);

    struct LegState {
        int hip;
        int knee;
        int ankle;
        QVector3D plantedFoot;
        float thighLength;
        float shinLength;
    };
    QVector<LegState> legs;
    if (jointIndex == static_cast<int>(HumanJoint::Pelvis)) {
        legs = {
            {static_cast<int>(HumanJoint::HipLeft), static_cast<int>(HumanJoint::KneeLeft), static_cast<int>(HumanJoint::AnkleLeft),
             asset.joints.at(static_cast<int>(HumanJoint::AnkleLeft)),
             (asset.joints.at(static_cast<int>(HumanJoint::KneeLeft)) - asset.joints.at(static_cast<int>(HumanJoint::HipLeft))).length(),
             (asset.joints.at(static_cast<int>(HumanJoint::AnkleLeft)) - asset.joints.at(static_cast<int>(HumanJoint::KneeLeft))).length()},
            {static_cast<int>(HumanJoint::HipRight), static_cast<int>(HumanJoint::KneeRight), static_cast<int>(HumanJoint::AnkleRight),
             asset.joints.at(static_cast<int>(HumanJoint::AnkleRight)),
             (asset.joints.at(static_cast<int>(HumanJoint::KneeRight)) - asset.joints.at(static_cast<int>(HumanJoint::HipRight))).length(),
             (asset.joints.at(static_cast<int>(HumanJoint::AnkleRight)) - asset.joints.at(static_cast<int>(HumanJoint::KneeRight))).length()}
        };

        // Keep pelvis movement inside the reach of both planted feet.
        float movementFraction = 1.0f;
        for (const LegState& leg : legs) {
            const float maximumReach = leg.thighLength + leg.shinLength;
            const QVector3D originalHip = asset.joints.at(leg.hip);
            const float requestedDistance = (leg.plantedFoot - (originalHip + delta)).length();
            if (requestedDistance > maximumReach) {
                float low = 0.0f;
                float high = movementFraction;
                for (int iteration = 0; iteration < 24; ++iteration) {
                    const float middle = (low + high) * 0.5f;
                    const float distance = (leg.plantedFoot - (originalHip + delta * middle)).length();
                    if (distance <= maximumReach) low = middle;
                    else high = middle;
                }
                movementFraction = std::min(movementFraction, low);
            }
        }
        delta *= movementFraction;
        constrainedPosition = asset.joints.at(jointIndex) + delta;
    }

    for (int index = 0; index < asset.joints.size(); ++index) {
        if (isJointInSubtree(jointIndex, index)) asset.joints[index] += delta;
    }

    if (jointIndex == static_cast<int>(HumanJoint::Pelvis)) {
        for (const LegState& leg : legs) {
            const QVector3D hip = asset.joints.at(leg.hip);
            const QVector3D ankle = leg.plantedFoot;
            const QVector3D toFoot = ankle - hip;
            const float distance = std::max(0.0001f, toFoot.length());
            const QVector3D direction = toFoot / distance;
            const float maxReach = leg.thighLength + leg.shinLength;
            const float clampedDistance = std::clamp(distance, std::abs(leg.thighLength - leg.shinLength) + 0.0001f, maxReach - 0.0001f);

            QVector3D bendDirection = asset.joints.at(leg.knee) - hip;
            bendDirection -= direction * QVector3D::dotProduct(bendDirection, direction);
            if (bendDirection.lengthSquared() < 0.0001f) {
                bendDirection = QVector3D::crossProduct(direction, QVector3D(0, 0, 1));
                if (bendDirection.lengthSquared() < 0.0001f) bendDirection = QVector3D::crossProduct(direction, QVector3D(0, 1, 0));
            }
            bendDirection.normalize();
            const float cosine = std::clamp((leg.thighLength * leg.thighLength + clampedDistance * clampedDistance - leg.shinLength * leg.shinLength) /
                                                (2.0f * leg.thighLength * clampedDistance), -1.0f, 1.0f);
            const float sine = std::sqrt(std::max(0.0f, 1.0f - cosine * cosine));
            asset.joints[leg.knee] = hip + direction * (cosine * leg.thighLength) + bendDirection * (sine * leg.thighLength);
            asset.joints[leg.ankle] = ankle;
        }
    }
}

QVector3D PaintingCanvas::assetSize(const PaintingAsset& asset) const {
    const float s = static_cast<float>(asset.scale);
    switch (asset.type) {
    case AssetType::Human: return QVector3D(1.85f, 4.25f, 1.25f) * s;
    case AssetType::Tree: return QVector3D(2.2f, 4.5f, 2.2f) * s;
    case AssetType::Mountain: return QVector3D(5.0f, 2.8f, 3.0f) * s;
    case AssetType::Rock: return QVector3D(2.6f, 1.2f, 1.8f) * s;
    case AssetType::Light: return QVector3D(2.2f, 2.2f, 2.2f) * s;
    case AssetType::TextBlock: return asset.textBlockDimensions;
    case AssetType::DowntownTower: return QVector3D(76.0f, 206.0f, 70.0f) * s;
    case AssetType::ResidentialNeighborhood: return QVector3D(72.0f, 66.0f, 48.0f) * s;
    case AssetType::ResidentialIntersection: return QVector3D(180.0f, 80.0f, 180.0f) * s;
    case AssetType::ResidentialAvenue: return QVector3D(310.0f, 80.0f, 82.0f) * s;
    case AssetType::Ocean: return QVector3D(300.0f, 1.2f, 300.0f) * s;
    case AssetType::GrandHarbour: return QVector3D(300.0f, 56.0f, 250.0f) * s;
    case AssetType::FacialExpression: return QVector3D(2.8f, 3.8f, 1.75f) * s;
    case AssetType::BaseballBat: return QVector3D(0.55f, 2.8f, 0.55f) * s;
    case AssetType::Pistol: return QVector3D(1.8f, 0.9f, 0.65f) * s;
    }
    return QVector3D(1.0f, 1.0f, 1.0f) * s;
}

QVector3D PaintingCanvas::assetBoundsMin(const PaintingAsset& asset) const {
    if (asset.type == AssetType::Human) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-0.78f, 0.0f, -0.48f) * s;
    }
    if (asset.type == AssetType::DowntownTower) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-38.0f, 0.0f, -35.0f) * s;
    }
    if (asset.type == AssetType::ResidentialNeighborhood) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-36.0f, 0.0f, -24.0f) * s;
    }
    if (asset.type == AssetType::ResidentialIntersection) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-90.0f, 0.0f, -90.0f) * s;
    }
    if (asset.type == AssetType::ResidentialAvenue) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-155.0f, 0.0f, -41.0f) * s;
    }
    if (asset.type == AssetType::Ocean) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-150.0f, -0.05f, -150.0f) * s;
    }
    if (asset.type == AssetType::GrandHarbour) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-150.0f, -0.5f, -70.0f) * s;
    }
    if (asset.type == AssetType::FacialExpression) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-1.4f, 0.0f, -0.88f) * s;
    }
    if (asset.type == AssetType::BaseballBat) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-0.28f, 0.0f, -0.28f) * s;
    }
    if (asset.type == AssetType::Pistol) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(-0.9f, 0.0f, -0.32f) * s;
    }
    if (asset.type == AssetType::TextBlock) {
        const QVector3D dimensions = asset.textBlockDimensions;
        if (asset.textFlat) return asset.position + QVector3D(-dimensions.x() * 0.5f, 0.0f, -dimensions.y() * 0.5f);
        return asset.position + QVector3D(-dimensions.x() * 0.5f, 0.0f, -dimensions.z() * 0.5f);
    }
    return asset.position;
}

QVector3D PaintingCanvas::assetBoundsMax(const PaintingAsset& asset) const {
    if (asset.type == AssetType::Human) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(0.78f, 4.25f, 0.48f) * s;
    }
    if (asset.type == AssetType::DowntownTower) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(38.0f, 206.0f, 35.0f) * s;
    }
    if (asset.type == AssetType::ResidentialNeighborhood) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(36.0f, 66.0f, 24.0f) * s;
    }
    if (asset.type == AssetType::ResidentialIntersection) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(90.0f, 80.0f, 90.0f) * s;
    }
    if (asset.type == AssetType::ResidentialAvenue) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(155.0f, 82.0f, 41.0f) * s;
    }
    if (asset.type == AssetType::Ocean) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(150.0f, 1.15f, 150.0f) * s;
    }
    if (asset.type == AssetType::GrandHarbour) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(150.0f, 56.0f, 180.0f) * s;
    }
    if (asset.type == AssetType::FacialExpression) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(1.4f, 3.8f, 0.88f) * s;
    }
    if (asset.type == AssetType::BaseballBat) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(0.28f, 2.8f, 0.28f) * s;
    }
    if (asset.type == AssetType::Pistol) {
        const float s = static_cast<float>(asset.scale);
        return asset.position + QVector3D(0.9f, 0.9f, 0.32f) * s;
    }
    if (asset.type == AssetType::TextBlock) {
        const QVector3D dimensions = asset.textBlockDimensions;
        if (asset.textFlat) return asset.position + QVector3D(dimensions.x() * 0.5f, dimensions.z() * 1.35f, dimensions.y() * 0.5f);
        return asset.position + QVector3D(dimensions.x() * 0.5f, dimensions.y(), dimensions.z() * 0.85f);
    }
    return asset.position + assetSize(asset);
}

void PaintingCanvas::addAsset(AssetType type, const QString& variant) {
    PaintingAsset asset;
    asset.id = myNextAssetId++;
    asset.name = assetName(type);
    asset.type = type;
    if (type == AssetType::FacialExpression) {
        const QStringList names = facialExpressionNames();
        asset.expression = names.contains(variant) ? variant : names.first();
        asset.name = asset.expression.left(1).toUpper() + asset.expression.mid(1) + QStringLiteral(" facial expression");
    }
    asset.position = defaultAssetPosition(type);
    if (type == AssetType::DowntownTower) asset.scale = 0.04;
    if (type == AssetType::ResidentialNeighborhood) asset.scale = 0.08;
    if (type == AssetType::ResidentialIntersection) asset.scale = 0.045;
    if (type == AssetType::ResidentialAvenue) asset.scale = 0.04;
    if (type == AssetType::Ocean) asset.scale = 0.06;
    if (type == AssetType::GrandHarbour) asset.scale = 0.045;
    if (type == AssetType::Human) asset.joints = defaultHumanJoints();
    if (type == AssetType::BaseballBat || type == AssetType::Pistol) {
        int humanId = -1;
        const PaintingAsset* selected = selectedAsset();
        if (selected && selected->type == AssetType::Human) humanId = selected->id;
        if (humanId < 0) {
            for (auto iterator = myAssets.crbegin(); iterator != myAssets.crend(); ++iterator) {
                if (iterator->type == AssetType::Human) {
                    humanId = iterator->id;
                    break;
                }
            }
        }
        if (humanId >= 0) {
            asset.attachedHumanId = humanId;
            for (PaintingAsset& human : myAssets) {
                if (human.id != humanId || human.joints.size() != humanJointCount()) continue;
                const auto setJoint = [&](HumanJoint joint, const QVector3D& position) {
                    human.joints[static_cast<int>(joint)] = position;
                };
                if (type == AssetType::BaseballBat) {
                    setJoint(HumanJoint::ElbowLeft, QVector3D(-0.42f, 2.70f, -0.16f));
                    setJoint(HumanJoint::WristLeft, QVector3D(-0.10f, 2.12f, -0.54f));
                    setJoint(HumanJoint::ElbowRight, QVector3D(0.42f, 2.70f, -0.16f));
                    setJoint(HumanJoint::WristRight, QVector3D(0.10f, 2.00f, -0.54f));
                } else {
                    setJoint(HumanJoint::ElbowLeft, QVector3D(-0.10f, 2.60f, -0.20f));
                    setJoint(HumanJoint::WristLeft, QVector3D(0.38f, 2.34f, -0.54f));
                    setJoint(HumanJoint::ElbowRight, QVector3D(0.78f, 2.80f, -0.18f));
                    setJoint(HumanJoint::WristRight, QVector3D(0.58f, 2.54f, -0.54f));
                }
                break;
            }
        }
    }
    myAssets.push_back(asset);
    mySelectedAssetId = asset.id;
    emit assetsChanged();
    emit selectionChanged(mySelectedAssetId);
    emit documentChanged();
    update();
}

const PaintingAsset* PaintingCanvas::selectedAsset() const {
    for (const PaintingAsset& asset : myAssets) if (asset.id == mySelectedAssetId) return &asset;
    return nullptr;
}

QVector3D PaintingCanvas::selectedAssetPosition() const {
    const PaintingAsset* asset = selectedAsset();
    return asset ? asset->position : QVector3D();
}

void PaintingCanvas::setSelectedAssetPosition(const QVector3D& position) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId) {
            asset.position = position;
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::selectAsset(int id) {
    if (id == mySelectedAssetId) return;
    mySelectedAssetId = id;
    mySelectedJointIndex = -1;
    emit selectionChanged(mySelectedAssetId);
    update();
}

void PaintingCanvas::selectHumanJoint(int jointIndex) {
    const PaintingAsset* asset = selectedAsset();
    if (!asset || asset->type != AssetType::Human || jointIndex < 0 || jointIndex >= asset->joints.size() ||
        jointIndex == static_cast<int>(HumanJoint::Neck)) return;
    mySelectedJointIndex = jointIndex;
    emit selectionChanged(mySelectedAssetId);
    update();
}

void PaintingCanvas::setAssetVisible(int id, bool visible) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == id) {
            asset.visible = visible;
            if (!visible && asset.id == mySelectedAssetId) {
                mySelectedAssetId = -1;
                mySelectedJointIndex = -1;
                emit selectionChanged(-1);
            }
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::setSelectedAssetScale(double scale) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId) {
            asset.scale = std::clamp(scale, 0.25, 100.0);
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::setSelectedAssetText(const QString& text) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId && asset.type == AssetType::TextBlock) {
            const QString trimmed = text.trimmed();
            asset.text = trimmed.isEmpty() ? "TEXT" : trimmed.left(8).toUpper();
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::setSelectedHumanExpression(const QString& expression) {
    const QStringList names = facialExpressionNames();
    for (PaintingAsset& asset : myAssets) {
        if (asset.id != mySelectedAssetId || asset.type != AssetType::Human) continue;
        if (expression == QStringLiteral("none") || names.contains(expression)) {
            asset.expression = expression;
            emit assetsChanged();
            emit documentChanged();
            update();
        }
        return;
    }
}

void PaintingCanvas::setSelectedAssetFont(const QString& family, bool italic, bool bold) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId && asset.type == AssetType::TextBlock) {
            asset.fontFamily = family.trimmed().isEmpty() ? QStringLiteral("Arial") : family;
            asset.fontItalic = italic;
            asset.fontBold = bold;
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::setSelectedTextBlockOrientation(bool vertical, bool flat) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId && asset.type == AssetType::TextBlock) {
            asset.textVertical = vertical;
            asset.textFlat = flat;
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

QVector3D PaintingCanvas::selectedTextBlockDimensions() const {
    const PaintingAsset* asset = selectedAsset();
    if (!asset || asset->type != AssetType::TextBlock) return QVector3D(4.4f, 1.8f, 0.38f);
    return asset->textBlockDimensions;
}

void PaintingCanvas::setSelectedTextBlockDimensions(const QVector3D& dimensions) {
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId && asset.type == AssetType::TextBlock) {
            asset.textBlockDimensions = QVector3D(
                std::clamp(dimensions.x(), 0.5f, 12.0f),
                std::clamp(dimensions.y(), 0.5f, 8.0f),
                std::clamp(dimensions.z(), 0.1f, 12.0f));
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

QVector3D PaintingCanvas::selectedHumanJointPosition(int jointIndex) const {
    const PaintingAsset* asset = selectedAsset();
    if (!asset || asset->type != AssetType::Human || jointIndex < 0 || jointIndex >= asset->joints.size()) return QVector3D();
    return asset->joints.at(jointIndex);
}

void PaintingCanvas::setSelectedHumanJointPosition(int jointIndex, const QVector3D& position) {
    if (jointIndex == static_cast<int>(HumanJoint::Neck)) return;
    for (PaintingAsset& asset : myAssets) {
        if (asset.id == mySelectedAssetId && asset.type == AssetType::Human && jointIndex >= 0 && jointIndex < asset.joints.size()) {
            moveJointSubtree(asset, jointIndex, position);
            emit assetsChanged();
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::removeSelectedAsset() {
    if (mySelectedAssetId < 0) return;
    for (int i = 0; i < myAssets.size(); ++i) {
        if (myAssets.at(i).id == mySelectedAssetId) {
            myAssets.removeAt(i);
            mySelectedAssetId = -1;
            mySelectedJointIndex = -1;
            emit assetsChanged();
            emit selectionChanged(-1);
            emit documentChanged();
            update();
            return;
        }
    }
}

void PaintingCanvas::clearStrokes() {
    myStrokes.clear();
    update();
    emit documentChanged();
}

void PaintingCanvas::newConcept() {
    myAssets.clear();
    myStrokes.clear();
    mySelectedAssetId = -1;
    mySelectedJointIndex = -1;
    emit assetsChanged();
    emit selectionChanged(-1);
    emit documentChanged();
    update();
}

QImage PaintingCanvas::renderCurrentView() {
    const bool previousRenderMode = myRenderMode;
    myRenderMode = true;
    repaint();
    const QImage image = grabFramebuffer();
    myRenderMode = previousRenderMode;
    update();
    return image;
}

void PaintingCanvas::initializeGL() {
    initializeOpenGLFunctions();
    glEnable(GL_DEPTH_TEST);
    glDepthFunc(GL_LESS);
    glDepthMask(GL_TRUE);
    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    myProgram = new QOpenGLShaderProgram(this);
    myProgram->addShaderFromSourceCode(QOpenGLShader::Vertex,
        "attribute vec3 aPosition;\n"
        "attribute vec3 aColor;\n"
        "attribute vec3 aNormal;\n"
        "attribute vec4 aJoints;\n"
        "attribute vec4 aWeights;\n"
        "uniform mat4 uMvp;\n"
        "uniform int uUseSkinning;\n"
        "uniform mat4 uBones[32];\n"
        "varying vec3 vColor;\n"
        "varying vec3 vNormal;\n"
        "varying vec3 vPosition;\n"
        "void main() {\n"
        "  vec4 position = vec4(aPosition, 1.0);\n"
        "  vec3 normal = aNormal;\n"
        "  if (uUseSkinning == 1) {\n"
        "    mat4 skin = aWeights.x * uBones[int(aJoints.x)] + aWeights.y * uBones[int(aJoints.y)] +\n"
        "                aWeights.z * uBones[int(aJoints.z)] + aWeights.w * uBones[int(aJoints.w)];\n"
        "    position = skin * position;\n"
        "    normal = (skin * vec4(normal, 0.0)).xyz;\n"
        "  }\n"
        "  gl_Position = uMvp * position; vColor = aColor; vNormal = normalize(normal); vPosition = position.xyz;\n"
        "}");
    myProgram->addShaderFromSourceCode(QOpenGLShader::Fragment,
        "varying vec3 vColor;\n"
        "varying vec3 vNormal;\n"
        "varying vec3 vPosition;\n"
        "float noise(vec3 p) { return fract(sin(dot(p, vec3(17.13, 41.71, 93.17))) * 43758.5453); }\n"
        "void main() {\n"
        "  vec3 n = normalize(vNormal);\n"
        "  vec3 lightDir = normalize(vec3(-0.42, 0.82, 0.54));\n"
        "  vec3 fillDir = normalize(vec3(0.62, 0.28, -0.72));\n"
        "  vec3 viewDir = normalize(vec3(3.5, 4.5, 7.0) - vPosition);\n"
        "  float wrapped = max(0.0, (dot(n, lightDir) + 0.32) / 1.32);\n"
        "  float fill = max(0.0, dot(n, fillDir));\n"
        "  float skin = step(0.035, vColor.r - vColor.b) * step(0.015, vColor.g - vColor.b);\n"
        "  float subsurface = skin * pow(max(0.0, 1.0 - dot(n, lightDir)), 2.0) * 0.22;\n"
        "  float fresnel = pow(1.0 - max(0.0, dot(n, viewDir)), 5.0);\n"
        "  float specular = pow(max(0.0, dot(reflect(-lightDir, n), viewDir)), 34.0) * (0.16 + 0.16 * skin);\n"
        "  float pore = (noise(vPosition * 34.0) - 0.5) * 0.026 * skin;\n"
        "  vec3 lit = vColor * (0.22 + wrapped * 0.64 + fill * 0.14);\n"
        "  lit += vec3(0.25, 0.055, 0.035) * subsurface;\n"
        "  lit += vec3(1.0, 0.72, 0.56) * (specular + fresnel * 0.028 * skin);\n"
        "  lit *= 1.0 + pore;\n"
        "  gl_FragColor = vec4(clamp(lit, 0.0, 1.0), 1.0);\n"
        "}");
    myProgram->bindAttributeLocation("aPosition", 0);
    myProgram->bindAttributeLocation("aColor", 1);
    myProgram->bindAttributeLocation("aNormal", 2);
    myProgram->bindAttributeLocation("aJoints", 3);
    myProgram->bindAttributeLocation("aWeights", 4);
    myProgram->link();
    myVertexBuffer.create();
}

void PaintingCanvas::resizeGL(int width, int height) {
    glViewport(0, 0, width, height);
    myProjection.setToIdentity();
    myProjection.perspective(45.0f, height > 0 ? float(width) / float(height) : 1.0f, 0.1f, 2000.0f);
}

QMatrix4x4 PaintingCanvas::viewMatrix() const {
    const float distance = static_cast<float>(myDistance);
    const QVector3D eye = myTarget + QVector3D(
        std::sin(myYaw) * std::cos(myPitch) * distance,
        std::sin(myPitch) * distance,
        std::cos(myYaw) * std::cos(myPitch) * distance);
    QMatrix4x4 view;
    view.lookAt(eye, myTarget, QVector3D(0.0f, 1.0f, 0.0f));
    return view;
}

void PaintingCanvas::paintGL() {
    const QVector3D background = colorVector(myBackgroundColor);
    glClearColor(background.x() * 0.36f, background.y() * 0.36f, background.z() * 0.36f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
    if (!myProgram || !myProgram->isLinked()) return;

    myProgram->bind();
    drawGround();

    for (const Stroke& stroke : myStrokes) {
        if (stroke.points.size() < 2) continue;
        QVector<Vertex> vertices;
        const QVector3D fallback = mixColor(colorVector(stroke.color), background, 0.32);
        const QVector3D strokeColor = stroke.eraser ? background : fallback;
        for (const QVector3D& point : stroke.points) vertices.push_back({point + QVector3D(0, 0.035f, 0), strokeColor});
        glLineWidth(static_cast<GLfloat>(std::clamp(stroke.width / 3.0, 1.0, 6.0)));
        drawVertices(vertices, GL_LINE_STRIP);
    }

    if (myCurrentStroke.points.size() >= 2) {
        QVector<Vertex> vertices;
        const QVector3D strokeColor = myCurrentStroke.eraser ? background : colorVector(myCurrentStroke.color);
        for (const QVector3D& point : myCurrentStroke.points) vertices.push_back({point + QVector3D(0, 0.04f, 0), strokeColor});
        drawVertices(vertices, GL_LINE_STRIP);
    }

    // Draw opaque assets from the camera's far plane toward the camera. The
    // depth buffer still handles individual surfaces, while this ordering
    // prevents asset insertion order from deciding which whole asset wins on
    // platforms with an unreliable depth attachment.
    const QMatrix4x4 view = viewMatrix();
    QVector<const PaintingAsset*> visibleAssets;
    for (const PaintingAsset& asset : myAssets) {
        if (asset.visible) visibleAssets.push_back(&asset);
    }
    std::stable_sort(visibleAssets.begin(), visibleAssets.end(), [&view](const PaintingAsset* first,
                                                                          const PaintingAsset* second) {
        const float firstDepth = (view * QVector4D(first->position, 1.0f)).z();
        const float secondDepth = (view * QVector4D(second->position, 1.0f)).z();
        return firstDepth < secondDepth;
    });
    for (const PaintingAsset* asset : visibleAssets) drawAsset(*asset);
    if (!myRenderMode) {
        if (const PaintingAsset* selected = selectedAsset()) {
            if (selected->visible) {
                drawSelection(*selected);
                if (selected->type == AssetType::Human) drawHumanJointHandles(*selected);
            }
        }
    }
    myProgram->release();

    if (!myRenderMode) {
        QPainter overlay(this);
        overlay.setRenderHint(QPainter::Antialiasing);
        drawOrientationGizmo(overlay);
    }
}

void PaintingCanvas::drawVertices(const QVector<Vertex>& vertices, GLenum mode,
                                  const QVector<QMatrix4x4>* skinMatrices) {
    if (vertices.isEmpty()) return;
    myVertexBuffer.bind();
    myVertexBuffer.allocate(vertices.constData(), vertices.size() * int(sizeof(Vertex)));
    myProgram->setUniformValue("uMvp", myProjection * viewMatrix());
    const bool useSkinning = skinMatrices && !skinMatrices->isEmpty();
    myProgram->setUniformValue("uUseSkinning", useSkinning ? 1 : 0);
    if (useSkinning) myProgram->setUniformValueArray("uBones", skinMatrices->constData(), skinMatrices->size());
    myProgram->enableAttributeArray(0);
    myProgram->enableAttributeArray(1);
    myProgram->enableAttributeArray(2);
    myProgram->enableAttributeArray(3);
    myProgram->enableAttributeArray(4);
    myProgram->setAttributeBuffer(0, GL_FLOAT, offsetof(Vertex, position), 3, sizeof(Vertex));
    myProgram->setAttributeBuffer(1, GL_FLOAT, offsetof(Vertex, color), 3, sizeof(Vertex));
    myProgram->setAttributeBuffer(2, GL_FLOAT, offsetof(Vertex, normal), 3, sizeof(Vertex));
    myProgram->setAttributeBuffer(3, GL_FLOAT, offsetof(Vertex, joints), 4, sizeof(Vertex));
    myProgram->setAttributeBuffer(4, GL_FLOAT, offsetof(Vertex, weights), 4, sizeof(Vertex));
    glDrawArrays(mode, 0, vertices.size());
    myProgram->disableAttributeArray(0);
    myProgram->disableAttributeArray(1);
    myProgram->disableAttributeArray(2);
    myProgram->disableAttributeArray(3);
    myProgram->disableAttributeArray(4);
    myVertexBuffer.release();
}

void PaintingCanvas::drawGround() {
    if (myRenderMode && !myGroundPlaneVisible) return;

    const QVector3D base = colorVector(myBackgroundColor);
    const QVector3D floor = mixColor(base, QVector3D(0.12f, 0.15f, 0.17f), 0.78);
    const float extent = myRenderMode ? 180.0f : 9.0f;
    QVector<Vertex> plane;
    addBox(plane, QVector3D(0, -0.08f, 0), QVector3D(extent * 2.0f, 0.1f, extent * 2.0f), floor);
    drawVertices(plane, GL_TRIANGLES);

    QVector<Vertex> grid;
    const int gridExtent = static_cast<int>(extent);
    const QVector3D gridColor = mixColor(base, QVector3D(0.65f, 0.7f, 0.72f), 0.25);
    for (int i = -gridExtent; i <= gridExtent; ++i) {
        addLine(grid, QVector3D(float(i), 0.0f, -extent), QVector3D(float(i), 0.0f, extent), gridColor);
        addLine(grid, QVector3D(-extent, 0.0f, float(i)), QVector3D(extent, 0.0f, float(i)), gridColor);
    }
    glLineWidth(1.0f);
    drawVertices(grid, GL_LINES);
}

QPointF PaintingCanvas::projectToScreen(const QVector3D& worldPoint) const {
    const QVector4D clip = (myProjection * viewMatrix()) * QVector4D(worldPoint, 1.0f);
    if (qFuzzyIsNull(clip.w())) return QPointF(-10000, -10000);
    const QVector3D ndc = clip.toVector3DAffine();
    return QPointF((ndc.x() + 1.0) * 0.5 * width(), (1.0 - ndc.y()) * 0.5 * height());
}

bool PaintingCanvas::screenToGround(const QPointF& screenPoint, QVector3D& groundPoint) const {
    const float x = float(2.0 * screenPoint.x() / std::max(1, width()) - 1.0);
    const float y = float(1.0 - 2.0 * screenPoint.y() / std::max(1, height()));
    bool invertible = false;
    const QMatrix4x4 inverse = (myProjection * viewMatrix()).inverted(&invertible);
    if (!invertible) return false;
    QVector4D nearPoint = inverse * QVector4D(x, y, -1.0f, 1.0f);
    QVector4D farPoint = inverse * QVector4D(x, y, 1.0f, 1.0f);
    nearPoint /= nearPoint.w();
    farPoint /= farPoint.w();
    const QVector3D origin = nearPoint.toVector3D();
    const QVector3D direction = (farPoint.toVector3D() - origin).normalized();
    if (std::abs(direction.y()) < 0.0001f) return false;
    const float distance = -origin.y() / direction.y();
    if (distance < 0.0f) return false;
    groundPoint = origin + direction * distance;
    return true;
}

bool PaintingCanvas::screenToPlane(const QPointF& screenPoint, const QVector3D& planePoint,
                                   const QVector3D& planeNormal, QVector3D& worldPoint) const {
    const float x = float(2.0 * screenPoint.x() / std::max(1, width()) - 1.0);
    const float y = float(1.0 - 2.0 * screenPoint.y() / std::max(1, height()));
    bool invertible = false;
    const QMatrix4x4 inverse = (myProjection * viewMatrix()).inverted(&invertible);
    if (!invertible) return false;
    QVector4D nearPoint = inverse * QVector4D(x, y, -1.0f, 1.0f);
    QVector4D farPoint = inverse * QVector4D(x, y, 1.0f, 1.0f);
    nearPoint /= nearPoint.w();
    farPoint /= farPoint.w();
    const QVector3D origin = nearPoint.toVector3D();
    const QVector3D direction = (farPoint.toVector3D() - origin).normalized();
    const float denominator = QVector3D::dotProduct(direction, planeNormal);
    if (std::abs(denominator) < 0.0001f) return false;
    const float distance = QVector3D::dotProduct(planePoint - origin, planeNormal) / denominator;
    if (distance < 0.0f) return false;
    worldPoint = origin + direction * distance;
    return true;
}

int PaintingCanvas::assetAt(const QPointF& screenPoint) const {
    int hit = -1;
    double nearest = 1e9;
    for (const PaintingAsset& asset : myAssets) {
        if (!asset.visible) continue;
        const QVector3D boundsMin = assetBoundsMin(asset);
        const QVector3D boundsMax = assetBoundsMax(asset);
        const QPointF center = projectToScreen((boundsMin + boundsMax) * 0.5f);
        const QPointF minCorner = projectToScreen(boundsMin);
        const QPointF maxCorner = projectToScreen(boundsMax);
        const double radius = std::max({18.0, std::hypot(minCorner.x() - center.x(), minCorner.y() - center.y()),
                                        std::hypot(maxCorner.x() - center.x(), maxCorner.y() - center.y())});
        const double dx = screenPoint.x() - center.x();
        const double dy = screenPoint.y() - center.y();
        const double distance = std::sqrt(dx * dx + dy * dy);
        if (distance < radius && distance < nearest) { nearest = distance; hit = asset.id; }
    }
    return hit;
}

int PaintingCanvas::humanJointAt(const QPointF& screenPoint, int& jointIndex) const {
    int hitAsset = -1;
    int hitJoint = -1;
    double nearest = 30.0;
    for (const PaintingAsset& asset : myAssets) {
        if (!asset.visible || asset.type != AssetType::Human) continue;
        for (int index = 0; index < asset.joints.size(); ++index) {
            if (index == static_cast<int>(HumanJoint::Neck)) continue;
            const QPointF jointScreen = projectToScreen(asset.position + asset.joints.at(index) * float(asset.scale));
            const double distance = std::hypot(screenPoint.x() - jointScreen.x(), screenPoint.y() - jointScreen.y());
            const bool isPelvis = index == static_cast<int>(HumanJoint::Pelvis);
            const double selectionDistance = distance + (isPelvis ? 1.5 : 0.0);
            if (selectionDistance < nearest) {
                nearest = selectionDistance;
                hitAsset = asset.id;
                hitJoint = index;
            }
        }
    }
    jointIndex = hitJoint;
    return hitAsset;
}

QRectF PaintingCanvas::orientationGizmoRect() const {
    return QRectF(width() - 136.0, 16.0, 120.0, 120.0);
}

PaintingCanvas::OrientationAxis PaintingCanvas::orientationAxisAt(const QPointF& screenPoint) const {
    const QRectF bounds = orientationGizmoRect();
    if (!bounds.contains(screenPoint)) return OrientationAxis::None;

    const QPointF center = bounds.center();
    const QMatrix4x4 view = viewMatrix();
    const QVector3D axes[] = {QVector3D(1, 0, 0), QVector3D(0, 1, 0), QVector3D(0, 0, 1)};
    const OrientationAxis positive[] = {OrientationAxis::X, OrientationAxis::Y, OrientationAxis::Z};
    const OrientationAxis negative[] = {OrientationAxis::NegativeX, OrientationAxis::NegativeY, OrientationAxis::NegativeZ};
    double nearest = 18.0;
    OrientationAxis result = OrientationAxis::None;
    for (int i = 0; i < 3; ++i) {
        const QVector3D direction = view.mapVector(axes[i]).normalized();
        const QPointF endpoint = center + QPointF(direction.x(), -direction.y()) * 40.0;
        const QPointF opposite = center - QPointF(direction.x(), -direction.y()) * 30.0;
        const double endpointDistance = std::hypot(screenPoint.x() - endpoint.x(), screenPoint.y() - endpoint.y());
        if (endpointDistance < nearest) {
            nearest = endpointDistance;
            result = positive[i];
        }
        const double oppositeDistance = std::hypot(screenPoint.x() - opposite.x(), screenPoint.y() - opposite.y());
        if (oppositeDistance < nearest) {
            nearest = oppositeDistance;
            result = negative[i];
        }
    }
    return result;
}

void PaintingCanvas::drawOrientationGizmo(QPainter& painter) const {
    const QRectF bounds = orientationGizmoRect();
    const QPointF center = bounds.center();
    painter.save();
    painter.setPen(QPen(QColor(101, 115, 127, 220), 1));
    painter.setBrush(QColor(19, 24, 30, 225));
    painter.drawEllipse(bounds);

    const QMatrix4x4 view = viewMatrix();
    const QVector3D axes[] = {QVector3D(1, 0, 0), QVector3D(0, 1, 0), QVector3D(0, 0, 1)};
    const QColor colors[] = {QColor("#e45b57"), QColor("#72c477"), QColor("#619de5")};
    const QString labels[] = {"X", "Y", "Z"};
    for (int i = 0; i < 3; ++i) {
        const QVector3D direction = view.mapVector(axes[i]).normalized();
        const QPointF screenDirection(direction.x(), -direction.y());
        const QPointF positive = center + screenDirection * 40.0;
        const QPointF negative = center - screenDirection * 30.0;
        const QColor positiveColor = colors[i];
        const QColor negativeColor = colors[i].darker(175);

        painter.setPen(QPen(negativeColor, 2));
        painter.drawLine(center, negative);
        painter.setBrush(negativeColor);
        painter.drawEllipse(negative, 7, 7);

        painter.setPen(QPen(positiveColor, 3));
        painter.drawLine(center, positive);
        painter.setBrush(positiveColor);
        painter.drawEllipse(positive, 10, 10);
        painter.setPen(Qt::white);
        painter.setFont(QFont("Arial", 9, QFont::Bold));
        painter.drawText(QRectF(positive.x() - 8, positive.y() - 8, 16, 16), Qt::AlignCenter, labels[i]);
    }
    painter.setPen(Qt::NoPen);
    painter.setBrush(QColor("#d6dbe0"));
    painter.drawEllipse(center, 4, 4);
    painter.restore();
}

void PaintingCanvas::snapViewTo(OrientationAxis axis) {
    switch (axis) {
    case OrientationAxis::X: myYaw = kPi / 2.0; myPitch = 0.0; break;
    case OrientationAxis::NegativeX: myYaw = -kPi / 2.0; myPitch = 0.0; break;
    case OrientationAxis::Y: myYaw = 0.0; myPitch = 1.25; break;
    case OrientationAxis::NegativeY: myYaw = 0.0; myPitch = -1.25; break;
    case OrientationAxis::Z: myYaw = 0.0; myPitch = 0.0; break;
    case OrientationAxis::NegativeZ: myYaw = kPi; myPitch = 0.0; break;
    case OrientationAxis::None: return;
    }
    update();
}

void PaintingCanvas::mousePressEvent(QMouseEvent* event) {
    myLastMousePos = event->position();
    if (event->button() == Qt::RightButton) { myOrbiting = true; return; }
    if (event->button() == Qt::MiddleButton) { myPanning = true; return; }

    if (event->button() != Qt::LeftButton) return;
    if (orientationGizmoRect().contains(event->position())) {
        myOrientationDragging = true;
        const OrientationAxis viewAxis = orientationAxisAt(event->position());
        if (viewAxis != OrientationAxis::None) snapViewTo(viewAxis);
        return;
    }
    if (myTool == PaintingTool::Select) {
        int jointIndex = -1;
        const int jointAssetId = humanJointAt(event->position(), jointIndex);
        if (jointAssetId >= 0) {
            selectAsset(jointAssetId);
            selectHumanJoint(jointIndex);
            myDraggedAssetId = jointAssetId;
            myDraggedJointIndex = jointIndex;
            const PaintingAsset* asset = selectedAsset();
            myJointDragPlanePoint = asset->position + asset->joints.at(jointIndex) * float(asset->scale);
            const QVector3D eye = myTarget + QVector3D(
                std::sin(myYaw) * std::cos(myPitch) * float(myDistance),
                std::sin(myPitch) * float(myDistance),
                std::cos(myYaw) * std::cos(myPitch) * float(myDistance));
            myJointDragPlaneNormal = (myTarget - eye).normalized();
            return;
        }
        const int hit = assetAt(event->position());
        selectAsset(hit);
        if (hit >= 0) {
            myDraggedAssetId = hit;
            const PaintingAsset* asset = selectedAsset();
            if (asset) {
                myAssetDragPlanePoint = asset->position;
                QVector3D horizontalPoint;
                if (screenToPlane(event->position(), myAssetDragPlanePoint, QVector3D(0, 1, 0), horizontalPoint)) {
                    myAssetDragStartPoint = horizontalPoint;
                    myAssetDragStartPosition = asset->position;
                }
            }
        }
        return;
    }

    QVector3D ground;
    if (!screenToGround(event->position(), ground)) return;
    myCurrentStroke = Stroke();
    myCurrentStroke.color = myBrushColor;
    myCurrentStroke.width = myBrushSize;
    myCurrentStroke.opacity = myBrushOpacity;
    myCurrentStroke.eraser = myTool == PaintingTool::Eraser;
    myCurrentStroke.points.push_back(ground);
    update();
}

void PaintingCanvas::mouseMoveEvent(QMouseEvent* event) {
    const QPointF current = event->position();
    const QPointF delta = current - myLastMousePos;
    myLastMousePos = current;

    if (myOrientationDragging) {
        myYaw -= delta.x() * 0.008;
        myPitch = std::clamp(myPitch + delta.y() * 0.008, -1.42, 1.42);
        update();
        return;
    }
    if (myOrbiting) {
        myYaw -= delta.x() * 0.008;
        myPitch = std::clamp(myPitch + delta.y() * 0.008, -1.42, 1.42);
        update();
        return;
    }
    if (myPanning) {
        const QVector3D eye = myTarget + QVector3D(
            std::sin(myYaw) * std::cos(myPitch) * float(myDistance),
            std::sin(myPitch) * float(myDistance),
            std::cos(myYaw) * std::cos(myPitch) * float(myDistance));
        const QVector3D viewDirection = (myTarget - eye).normalized();
        const QVector3D right = QVector3D::crossProduct(viewDirection, QVector3D(0, 1, 0)).normalized();
        const QVector3D up = QVector3D::crossProduct(right, viewDirection).normalized();
        myTarget += (-right * float(delta.x() * myDistance * 0.0025) + up * float(delta.y() * myDistance * 0.0025));
        update();
        return;
    }
    if (myDraggedJointIndex >= 0 && myDraggedAssetId >= 0) {
        QVector3D worldPoint;
        if (screenToPlane(current, myJointDragPlanePoint, myJointDragPlaneNormal, worldPoint)) {
            for (PaintingAsset& asset : myAssets) {
                if (asset.id == myDraggedAssetId && myDraggedJointIndex < asset.joints.size()) {
                    moveJointSubtree(asset, myDraggedJointIndex,
                                     (worldPoint - asset.position) / float(asset.scale));
                    emit documentChanged();
                    update();
                    return;
                }
            }
        }
    }
    if (myDraggedAssetId >= 0) {
        QVector3D horizontalPoint;
        if (screenToPlane(current, myAssetDragPlanePoint, QVector3D(0, 1, 0), horizontalPoint)) {
            for (PaintingAsset& asset : myAssets) {
                if (asset.id == myDraggedAssetId) {
                    const QVector3D dragDelta = horizontalPoint - myAssetDragStartPoint;
                    asset.position = myAssetDragStartPosition + QVector3D(
                        static_cast<float>(dragDelta.x() * myAssetDragSensitivity), 0.0f,
                        static_cast<float>(dragDelta.z() * myAssetDragSensitivity));
                    emit documentChanged();
                    update();
                    return;
                }
            }
        }
    }
    if (!myCurrentStroke.points.isEmpty()) {
        QVector3D ground;
        if (screenToGround(current, ground)) {
            myCurrentStroke.points.push_back(ground);
            update();
        }
    }
}

void PaintingCanvas::mouseReleaseEvent(QMouseEvent*) {
    myOrientationDragging = false;
    myOrbiting = false;
    myPanning = false;
    if (myDraggedAssetId >= 0) {
        if (myDraggedJointIndex >= 0) {
            myDraggedJointIndex = -1;
            myDraggedAssetId = -1;
            emit assetsChanged();
            return;
        }
        myDraggedAssetId = -1;
        emit assetsChanged();
        return;
    }
    if (!myCurrentStroke.points.isEmpty()) {
        myStrokes.push_back(myCurrentStroke);
        myCurrentStroke = Stroke();
        emit documentChanged();
        update();
    }
}

void PaintingCanvas::wheelEvent(QWheelEvent* event) {
    const double previousDistance = myDistance;
    const double nextDistance = std::clamp(
        previousDistance * std::pow(0.9, event->angleDelta().y() / 120.0), 4.0, 500.0);

    // Dolly toward the ground point beneath the cursor while preserving the
    // current orbit direction. This makes the pointer act as the zoom anchor
    // instead of always pulling the scene toward its target center.
    QVector3D zoomAnchor;
    if (nextDistance != previousDistance && screenToGround(event->position(), zoomAnchor)) {
        const float targetShift = static_cast<float>(1.0 - nextDistance / previousDistance);
        myTarget += (zoomAnchor - myTarget) * targetShift;
    }
    myDistance = nextDistance;
    update();
}

void PaintingCanvas::addLine(QVector<Vertex>& vertices, const QVector3D& from, const QVector3D& to, const QVector3D& color) const {
    vertices.push_back({from, color});
    vertices.push_back({to, color});
}

void PaintingCanvas::addBox(QVector<Vertex>& vertices, const QVector3D& center, const QVector3D& size, const QVector3D& color) const {
    const QVector3D h = size * 0.5f;
    const QVector3D p[8] = {
        center + QVector3D(-h.x(), -h.y(), -h.z()), center + QVector3D(h.x(), -h.y(), -h.z()),
        center + QVector3D(h.x(), h.y(), -h.z()), center + QVector3D(-h.x(), h.y(), -h.z()),
        center + QVector3D(-h.x(), -h.y(), h.z()), center + QVector3D(h.x(), -h.y(), h.z()),
        center + QVector3D(h.x(), h.y(), h.z()), center + QVector3D(-h.x(), h.y(), h.z())};
    const int faces[36] = {0,1,2, 0,2,3, 1,5,6, 1,6,2, 5,4,7, 5,7,6, 4,0,3, 4,3,7, 3,2,6, 3,6,7, 4,5,1, 4,1,0};
    for (int face = 0; face < 36; face += 3) {
        const QVector3D normal = QVector3D::crossProduct(p[faces[face + 1]] - p[faces[face]],
                                                           p[faces[face + 2]] - p[faces[face]]).normalized();
        for (int corner = 0; corner < 3; ++corner) vertices.push_back({p[faces[face + corner]], color, normal});
    }
}

void PaintingCanvas::addSphere(QVector<Vertex>& vertices, const QVector3D& center, const QVector3D& radii, const QVector3D& color) const {
    constexpr int stacks = 8;
    constexpr int slices = 16;
    for (int stack = 0; stack < stacks; ++stack) {
        const float phi1 = kPi * float(stack) / stacks;
        const float phi2 = kPi * float(stack + 1) / stacks;
        for (int slice = 0; slice < slices; ++slice) {
            const float theta1 = 2.0f * kPi * float(slice) / slices;
            const float theta2 = 2.0f * kPi * float(slice + 1) / slices;
            const auto point = [&](float phi, float theta) {
                return center + QVector3D(radii.x() * std::sin(phi) * std::cos(theta), radii.y() * std::cos(phi), radii.z() * std::sin(phi) * std::sin(theta));
            };
            const QVector3D a = point(phi1, theta1), b = point(phi2, theta1), c = point(phi2, theta2), d = point(phi1, theta2);
            const auto normalAt = [&](const QVector3D& p) {
                return QVector3D((p.x() - center.x()) / std::max(0.0001f, radii.x()),
                                 (p.y() - center.y()) / std::max(0.0001f, radii.y()),
                                 (p.z() - center.z()) / std::max(0.0001f, radii.z())).normalized();
            };
            vertices.push_back({a, color, normalAt(a)}); vertices.push_back({b, color, normalAt(b)}); vertices.push_back({c, color, normalAt(c)});
            vertices.push_back({a, color, normalAt(a)}); vertices.push_back({c, color, normalAt(c)}); vertices.push_back({d, color, normalAt(d)});
        }
    }
}

void PaintingCanvas::addCone(QVector<Vertex>& vertices, const QVector3D& center, double radius, double height, const QVector3D& color) const {
    const QVector3D apex = center + QVector3D(0, float(height), 0);
    constexpr int slices = 16;
    for (int slice = 0; slice < slices; ++slice) {
        const float a1 = 2.0f * kPi * float(slice) / slices;
        const float a2 = 2.0f * kPi * float(slice + 1) / slices;
        const QVector3D p1 = center + QVector3D(float(radius * std::cos(a1)), 0, float(radius * std::sin(a1)));
        const QVector3D p2 = center + QVector3D(float(radius * std::cos(a2)), 0, float(radius * std::sin(a2)));
        const QVector3D radial1(std::cos(a1), 0.0f, std::sin(a1));
        const QVector3D radial2(std::cos(a2), 0.0f, std::sin(a2));
        const QVector3D n1 = (radial1 - QVector3D(0, float(radius / std::max(0.001, height)), 0)).normalized();
        const QVector3D n2 = (radial2 - QVector3D(0, float(radius / std::max(0.001, height)), 0)).normalized();
        vertices.push_back({p1, color, n1}); vertices.push_back({p2, color, n2}); vertices.push_back({apex, color, (n1 + n2).normalized()});
    }
}

void PaintingCanvas::addCylinder(QVector<Vertex>& vertices, const QVector3D& bottom, const QVector3D& top, double radius, const QVector3D& color) const {
    const QVector3D axis = top - bottom;
    const QVector3D direction = axis.normalized();
    QVector3D side = QVector3D::crossProduct(direction, QVector3D(0, 1, 0));
    if (side.lengthSquared() < 0.001f) side = QVector3D::crossProduct(direction, QVector3D(1, 0, 0));
    side.normalize();
    const QVector3D other = QVector3D::crossProduct(direction, side).normalized();
    constexpr int slices = 10;
    for (int slice = 0; slice < slices; ++slice) {
        const float a1 = 2.0f * kPi * float(slice) / slices;
        const float a2 = 2.0f * kPi * float(slice + 1) / slices;
        const QVector3D r1 = (side * std::cos(a1) + other * std::sin(a1)) * float(radius);
        const QVector3D r2 = (side * std::cos(a2) + other * std::sin(a2)) * float(radius);
        const QVector3D n1 = (r1 / std::max(0.0001f, float(radius))).normalized();
        const QVector3D n2 = (r2 / std::max(0.0001f, float(radius))).normalized();
        vertices.push_back({bottom + r1, color, n1}); vertices.push_back({bottom + r2, color, n2}); vertices.push_back({top + r2, color, n2});
        vertices.push_back({bottom + r1, color, n1}); vertices.push_back({top + r2, color, n2}); vertices.push_back({top + r1, color, n1});
    }
}

void PaintingCanvas::drawAsset(const PaintingAsset& asset) {
    switch (asset.type) {
    case AssetType::Human: drawHuman(asset); break;
    case AssetType::Tree: drawTree(asset.position, asset.scale); break;
    case AssetType::Mountain: drawMountain(asset.position, asset.scale); break;
    case AssetType::Rock: drawRock(asset.position, asset.scale); break;
    case AssetType::Light: drawLight(asset.position, asset.scale); break;
    case AssetType::TextBlock: drawTextBlock(asset); break;
    case AssetType::DowntownTower: drawDowntownTower(asset); break;
    case AssetType::ResidentialNeighborhood: drawResidentialNeighborhood(asset); break;
    case AssetType::ResidentialIntersection: drawResidentialIntersection(asset); break;
    case AssetType::ResidentialAvenue: drawResidentialAvenue(asset); break;
    case AssetType::Ocean: drawOcean(asset); break;
    case AssetType::GrandHarbour: drawGrandHarbour(asset); break;
    case AssetType::FacialExpression: drawFacialExpression(asset); break;
    case AssetType::BaseballBat: drawBaseballBat(asset); break;
    case AssetType::Pistol: drawPistol(asset); break;
    }
}

void PaintingCanvas::drawHuman(const PaintingAsset& asset) {
    // Keep this asset deliberately schematic: the cylinders show the body
    // structure while the selected-joint handles provide the editable control
    // points. No imported mesh or facial detail is drawn here.
    QVector<Vertex> vertices;
    const float scale = float(asset.scale);
    const QVector<QVector3D> joints = asset.joints.size() == humanJointCount() ? asset.joints : defaultHumanJoints();
    const auto joint = [&](HumanJoint index) {
        return asset.position + joints.at(static_cast<int>(index)) * scale;
    };
    const QVector3D pelvis = joint(HumanJoint::Pelvis);
    const QVector3D chest = joint(HumanJoint::Chest);
    const QVector3D neck = joint(HumanJoint::Neck);
    const QVector3D head = joint(HumanJoint::Head);
    const QVector3D shoulderLeft = joint(HumanJoint::ShoulderLeft);
    const QVector3D elbowLeft = joint(HumanJoint::ElbowLeft);
    const QVector3D wristLeft = joint(HumanJoint::WristLeft);
    const QVector3D shoulderRight = joint(HumanJoint::ShoulderRight);
    const QVector3D elbowRight = joint(HumanJoint::ElbowRight);
    const QVector3D wristRight = joint(HumanJoint::WristRight);
    const QVector3D hipLeft = joint(HumanJoint::HipLeft);
    const QVector3D kneeLeft = joint(HumanJoint::KneeLeft);
    const QVector3D ankleLeft = joint(HumanJoint::AnkleLeft);
    const QVector3D hipRight = joint(HumanJoint::HipRight);
    const QVector3D kneeRight = joint(HumanJoint::KneeRight);
    const QVector3D ankleRight = joint(HumanJoint::AnkleRight);

    const QVector3D bodyColor(0.38f, 0.53f, 0.62f);
    const QVector3D torsoColor(0.32f, 0.47f, 0.56f);
    const QVector3D jointColor(0.90f, 0.58f, 0.20f);
    const auto bone = [&](const QVector3D& first, const QVector3D& second, float radius = 0.13f) {
        addCylinder(vertices, first, second, radius * scale, bodyColor);
    };
    const auto torsoBone = [&](const QVector3D& first, const QVector3D& second, float radius) {
        addCylinder(vertices, first, second, radius * scale, torsoColor);
    };
    const auto jointMarker = [&](const QVector3D& point, float radius = 0.11f) {
        addSphere(vertices, point, QVector3D(radius, radius, radius) * scale, jointColor);
    };

    const auto addTaperedTorso = [&]() {
        const QVector3D shoulderCenter = (shoulderLeft + shoulderRight) * 0.5f;
        const QVector3D hipCenter = (hipLeft + hipRight) * 0.5f;
        const float topY = shoulderCenter.y() - 0.10f * scale;
        const float bottomY = hipCenter.y() + 0.10f * scale;
        const float topHalfWidth = 0.65f * scale;
        const float bottomHalfWidth = 0.48f * scale;
        const float topHalfDepth = 0.34f * scale;
        const float bottomHalfDepth = 0.29f * scale;
        const float bevel = 0.10f * scale;
        constexpr int cornerSegments = 6;
        constexpr int perimeterSegments = cornerSegments * 4;
        const auto ring = [&](float y, float centerX, float centerZ, float halfWidth, float halfDepth, float corner) {
            QVector<QVector3D> points;
            points.reserve(perimeterSegments);
            const float safeCorner = std::min(corner, std::min(halfWidth, halfDepth) * 0.8f);
            for (int cornerIndex = 0; cornerIndex < 4; ++cornerIndex) {
                const QVector3D cornerCenter(
                    centerX + (cornerIndex == 0 || cornerIndex == 1 ? 1.0f : -1.0f) * (halfWidth - safeCorner),
                    y,
                    centerZ + (cornerIndex == 1 || cornerIndex == 2 ? 1.0f : -1.0f) * (halfDepth - safeCorner));
                const float startAngle = -kPi * 0.5f + cornerIndex * kPi * 0.5f;
                for (int segment = 0; segment < cornerSegments; ++segment) {
                    const float angle = startAngle + kPi * 0.5f * float(segment) / float(cornerSegments - 1);
                    points.push_back(cornerCenter + QVector3D(safeCorner * std::cos(angle), 0.0f,
                                                               safeCorner * std::sin(angle)));
                }
            }
            return points;
        };

        // Multiple rings round the top, bottom, and four vertical corners.
        // The main rings retain the broad-shoulder-to-hip taper.
        QVector<QVector<QVector3D>> rings;
        rings.push_back(ring(bottomY, hipCenter.x(), hipCenter.z(), bottomHalfWidth - bevel, bottomHalfDepth - bevel, bevel * 0.50f));
        rings.push_back(ring(bottomY + bevel * 0.35f, hipCenter.x(), hipCenter.z(), bottomHalfWidth - bevel * 0.35f,
                             bottomHalfDepth - bevel * 0.35f, bevel * 0.75f));
        rings.push_back(ring(bottomY + bevel, hipCenter.x(), hipCenter.z(), bottomHalfWidth, bottomHalfDepth, bevel));
        rings.push_back(ring(topY - bevel, shoulderCenter.x(), shoulderCenter.z(), topHalfWidth, topHalfDepth, bevel));
        rings.push_back(ring(topY - bevel * 0.35f, shoulderCenter.x(), shoulderCenter.z(), topHalfWidth - bevel * 0.35f,
                             topHalfDepth - bevel * 0.35f, bevel * 0.75f));
        rings.push_back(ring(topY, shoulderCenter.x(), shoulderCenter.z(), topHalfWidth - bevel, topHalfDepth - bevel, bevel * 0.50f));

        const auto smoothNormal = [&](int ringIndex, int pointIndex) {
            const int previousRing = std::max(0, ringIndex - 1);
            const int nextRing = std::min(int(rings.size()) - 1, ringIndex + 1);
            const int previousPoint = (pointIndex + perimeterSegments - 1) % perimeterSegments;
            const int nextPoint = (pointIndex + 1) % perimeterSegments;
            const QVector3D around = rings[ringIndex][nextPoint] - rings[ringIndex][previousPoint];
            const QVector3D along = rings[nextRing][pointIndex] - rings[previousRing][pointIndex];
            QVector3D normal = QVector3D::crossProduct(around, along).normalized();
            const QVector3D outward(rings[ringIndex][pointIndex].x() - shoulderCenter.x(), 0.0f,
                                    rings[ringIndex][pointIndex].z() - shoulderCenter.z());
            if (QVector3D::dotProduct(normal, outward) < 0.0f) normal = -normal;
            return normal;
        };
        const auto quad = [&](int ringIndex, int pointIndex, int nextPoint) {
            const int nextRing = ringIndex + 1;
            const QVector3D normalA = smoothNormal(ringIndex, pointIndex);
            const QVector3D normalB = smoothNormal(ringIndex, nextPoint);
            const QVector3D normalC = smoothNormal(nextRing, nextPoint);
            const QVector3D normalD = smoothNormal(nextRing, pointIndex);
            vertices.push_back({rings[ringIndex][pointIndex], torsoColor, normalA});
            vertices.push_back({rings[ringIndex][nextPoint], torsoColor, normalB});
            vertices.push_back({rings[nextRing][nextPoint], torsoColor, normalC});
            vertices.push_back({rings[ringIndex][pointIndex], torsoColor, normalA});
            vertices.push_back({rings[nextRing][nextPoint], torsoColor, normalC});
            vertices.push_back({rings[nextRing][pointIndex], torsoColor, normalD});
        };
        for (int ringIndex = 0; ringIndex + 1 < rings.size(); ++ringIndex) {
            for (int pointIndex = 0; pointIndex < perimeterSegments; ++pointIndex) {
                quad(ringIndex, pointIndex, (pointIndex + 1) % perimeterSegments);
            }
        }
    };

    // A tapered rectangular torso slopes from the broad shoulders down to the
    // narrower hip width, instead of reading as a vertical rectangular box.
    addTaperedTorso();
    torsoBone(chest, neck, 0.14f);

    // Rounded shoulder and hip caps make the limb transitions feel anatomical;
    // the thicker segment radii keep the mannequin from reading as a stick.
    addSphere(vertices, shoulderLeft, QVector3D(0.22f, 0.22f, 0.22f) * scale, bodyColor);
    addSphere(vertices, shoulderRight, QVector3D(0.22f, 0.22f, 0.22f) * scale, bodyColor);
    addSphere(vertices, hipLeft, QVector3D(0.25f, 0.25f, 0.25f) * scale, bodyColor);
    addSphere(vertices, hipRight, QVector3D(0.25f, 0.25f, 0.25f) * scale, bodyColor);
    torsoBone(shoulderLeft, shoulderRight, 0.10f);
    bone(shoulderLeft, elbowLeft, 0.15f); bone(elbowLeft, wristLeft, 0.12f);
    bone(shoulderRight, elbowRight, 0.15f); bone(elbowRight, wristRight, 0.12f);
    torsoBone(hipLeft, hipRight, 0.12f);
    bone(hipLeft, kneeLeft, 0.19f); bone(kneeLeft, ankleLeft, 0.14f);
    bone(hipRight, kneeRight, 0.19f); bone(kneeRight, ankleRight, 0.14f);

    // Simple palm and finger forms give the control body readable hands.
    const auto addHand = [&](const QVector3D& wrist, float side) {
        addSphere(vertices, wrist + QVector3D(0.0f, -0.06f, -0.01f) * scale,
                  QVector3D(0.16f, 0.18f, 0.14f) * scale, bodyColor);
        for (int finger = 0; finger < 4; ++finger) {
            const float offset = (float(finger) - 1.5f) * 0.065f;
            const float length = 0.22f + (finger % 2) * 0.025f;
            const QVector3D start = wrist + QVector3D(offset, -0.13f, -0.04f) * scale;
            const QVector3D end = wrist + QVector3D(offset * 1.15f + side * 0.015f,
                                                     -0.13f - length, -0.045f) * scale;
            addCylinder(vertices, start, end, 0.035 * scale, bodyColor);
            addSphere(vertices, end, QVector3D(0.04f, 0.04f, 0.04f) * scale, bodyColor);
        }
        const QVector3D thumbStart = wrist + QVector3D(side * 0.12f, -0.04f, -0.03f) * scale;
        const QVector3D thumbEnd = wrist + QVector3D(side * 0.22f, -0.16f, -0.08f) * scale;
        addCylinder(vertices, thumbStart, thumbEnd, 0.045 * scale, bodyColor);
        addSphere(vertices, thumbEnd, QVector3D(0.05f, 0.05f, 0.05f) * scale, bodyColor);
    };
    addHand(wristLeft, -1.0f);
    addHand(wristRight, 1.0f);

    // Rounded dark shoe forms extend slightly forward from each ankle.
    const QVector3D shoeColor(0.08f, 0.11f, 0.14f);
    const auto addShoe = [&](const QVector3D& ankle) {
        addSphere(vertices, ankle + QVector3D(0.0f, -0.08f, -0.10f) * scale,
                  QVector3D(0.22f, 0.14f, 0.38f) * scale, shoeColor);
        addSphere(vertices, ankle + QVector3D(0.0f, -0.12f, -0.25f) * scale,
                  QVector3D(0.20f, 0.10f, 0.20f) * scale, shoeColor);
    };
    addShoe(ankleLeft);
    addShoe(ankleRight);

    for (const QVector3D& point : {pelvis, chest, head, shoulderLeft, elbowLeft, wristLeft,
                                   shoulderRight, elbowRight, wristRight, hipLeft, kneeLeft,
                                   ankleLeft, hipRight, kneeRight, ankleRight}) {
        jointMarker(point);
    }
    drawVertices(vertices, GL_TRIANGLES);
    if (!drawHumanExpressionHead(asset, head)) {
        QVector<Vertex> fallbackHead;
        addSphere(fallbackHead, head, QVector3D(0.32f, 0.30f, 0.36f) * scale, bodyColor);
        drawVertices(fallbackHead, GL_TRIANGLES);
    }
    return;

    // The former detailed procedural body is kept below as an archival
    // fallback for future comparison, but is disabled by design.
#if 0
    // The figure is intentionally assembled from overlapping anatomical masses.
    // The joint graph stays separate, so posing remains stable while the visible
    // surface reads as a continuous body rather than a stick figure.
    const QVector3D skin(0.67f, 0.34f, 0.255f);
    const QVector3D skinLight(0.77f, 0.43f, 0.34f);
    const QVector3D skinWarm(0.73f, 0.32f, 0.255f);
    const QVector3D skinShadow(0.55f, 0.245f, 0.19f);
    const QVector3D crease(0.39f, 0.13f, 0.095f);
    const QVector3D eyeColor(0.028f, 0.018f, 0.014f);
    const auto midpoint = [](const QVector3D& first, const QVector3D& second) {
        return (first + second) * 0.5f;
    };
    const auto addLimb = [&](const QVector3D& first, const QVector3D& second, float radius,
                             const QVector3D& color, float jointRadius) {
        addCylinder(vertices, first, second, radius * scale, color);
        addSphere(vertices, first, QVector3D(jointRadius, jointRadius, jointRadius) * scale, color);
        addSphere(vertices, second, QVector3D(jointRadius, jointRadius, jointRadius) * scale, color);
    };

    // Rib cage, abdomen, pelvis and pectoral planes.
    addSphere(vertices, pelvis, QVector3D(0.52f, 0.31f, 0.34f) * scale, skinShadow);
    addSphere(vertices, midpoint(pelvis, chest) + QVector3D(0.0f, 0.08f, 0.0f),
              QVector3D(0.43f, 0.50f, 0.28f) * scale, skin);
    addSphere(vertices, chest, QVector3D(0.61f, 0.48f, 0.35f) * scale, skinLight);
    addCylinder(vertices, pelvis, chest, 0.32 * scale, skin);
    addCylinder(vertices, chest, neck, 0.19 * scale, skinShadow);
    addCylinder(vertices, shoulderLeft, shoulderRight, 0.13 * scale, skinShadow);
    addCylinder(vertices, hipLeft, hipRight, 0.16 * scale, skinShadow);

    const QVector3D pecLeft = chest + QVector3D(-0.25f, -0.07f, 0.25f) * scale;
    const QVector3D pecRight = chest + QVector3D(0.25f, -0.07f, 0.25f) * scale;
    addSphere(vertices, pecLeft, QVector3D(0.28f, 0.24f, 0.15f) * scale, skinWarm);
    addSphere(vertices, pecRight, QVector3D(0.28f, 0.24f, 0.15f) * scale, skinWarm);

    // A subtle central sternum and abdominal rhythm keep the large forms readable.
    addSphere(vertices, chest + QVector3D(0.0f, -0.17f, 0.28f) * scale,
              QVector3D(0.10f, 0.34f, 0.06f) * scale, skinShadow);
    for (int row = 0; row < 3; ++row) {
        const float width = row == 0 ? 0.14f : 0.16f;
        const QVector3D rowCenter = pelvis + (chest - pelvis) * (0.28f + row * 0.17f) + QVector3D(0.0f, 0.0f, 0.28f) * scale;
        addSphere(vertices, rowCenter + QVector3D(-width, 0.0f, 0.0f) * scale,
                  QVector3D(0.12f, 0.12f, 0.055f) * scale, skinLight);
        addSphere(vertices, rowCenter + QVector3D(width, 0.0f, 0.0f) * scale,
                  QVector3D(0.12f, 0.12f, 0.055f) * scale, skinLight);
    }

    // Shoulder caps, upper/lower arms and small hands.
    addSphere(vertices, shoulderLeft, QVector3D(0.29f, 0.27f, 0.27f) * scale, skinLight);
    addSphere(vertices, shoulderRight, QVector3D(0.29f, 0.27f, 0.27f) * scale, skinLight);
    addLimb(shoulderLeft, elbowLeft, 0.145f, skin, 0.17f);
    addLimb(elbowLeft, wristLeft, 0.105f, skinWarm, 0.13f);
    addLimb(shoulderRight, elbowRight, 0.145f, skin, 0.17f);
    addLimb(elbowRight, wristRight, 0.105f, skinWarm, 0.13f);
    addSphere(vertices, wristLeft + QVector3D(0, -0.06f, 0.04f) * scale, QVector3D(0.13f, 0.18f, 0.10f) * scale, skinLight);
    addSphere(vertices, wristRight + QVector3D(0, -0.06f, 0.04f) * scale, QVector3D(0.13f, 0.18f, 0.10f) * scale, skinLight);

    // Gluteal mass, thighs, knees, calves and feet.
    addSphere(vertices, hipLeft, QVector3D(0.27f, 0.29f, 0.28f) * scale, skinWarm);
    addSphere(vertices, hipRight, QVector3D(0.27f, 0.29f, 0.28f) * scale, skinWarm);
    addLimb(hipLeft, kneeLeft, 0.205f, skin, 0.22f);
    addLimb(kneeLeft, ankleLeft, 0.145f, skinWarm, 0.16f);
    addLimb(hipRight, kneeRight, 0.205f, skin, 0.22f);
    addLimb(kneeRight, ankleRight, 0.145f, skinWarm, 0.16f);
    addSphere(vertices, ankleLeft + QVector3D(0.0f, -0.05f, 0.13f) * scale,
              QVector3D(0.19f, 0.12f, 0.34f) * scale, skinShadow);
    addSphere(vertices, ankleRight + QVector3D(0.0f, -0.05f, 0.13f) * scale,
              QVector3D(0.19f, 0.12f, 0.34f) * scale, skinShadow);

    // Head, ears, facial planes and hair mass. The eyes are small but give the
    // silhouette a human focal point when the camera is close.
    addSphere(vertices, neck, QVector3D(0.22f, 0.24f, 0.22f) * scale, skinShadow);
    addSphere(vertices, head, QVector3D(0.29f, 0.36f, 0.28f) * scale, skinLight);
    addSphere(vertices, head + QVector3D(-0.27f, 0.0f, 0.0f) * scale, QVector3D(0.065f, 0.12f, 0.075f) * scale, skinWarm);
    addSphere(vertices, head + QVector3D(0.27f, 0.0f, 0.0f) * scale, QVector3D(0.065f, 0.12f, 0.075f) * scale, skinWarm);
    addSphere(vertices, head + QVector3D(0.0f, 0.18f, -0.02f) * scale, QVector3D(0.27f, 0.19f, 0.25f) * scale, crease);
    addSphere(vertices, head + QVector3D(-0.105f, 0.075f, 0.255f) * scale, QVector3D(0.034f, 0.038f, 0.024f) * scale, eyeColor);
    addSphere(vertices, head + QVector3D(0.105f, 0.075f, 0.255f) * scale, QVector3D(0.034f, 0.038f, 0.024f) * scale, eyeColor);
    addSphere(vertices, head + QVector3D(0.0f, 0.0f, 0.30f) * scale, QVector3D(0.045f, 0.09f, 0.095f) * scale, skinWarm);
    addSphere(vertices, head + QVector3D(0.0f, -0.14f, 0.265f) * scale, QVector3D(0.10f, 0.035f, 0.025f) * scale, crease);

    drawVertices(vertices, GL_TRIANGLES);
#endif
}

bool PaintingCanvas::drawHumanExpressionHead(const PaintingAsset& asset, const QVector3D& head) {
    if (asset.expression == QStringLiteral("none")) return false;
    const QStringList names = facialExpressionNames();
    const int index = names.indexOf(asset.expression);
    if (index < 0 || index >= myFacialExpressions.size() || !myFacialExpressions.at(index).loaded) return false;

    const DowntownBuilding& model = myFacialExpressions.at(index);
    QVector<Vertex> transformed;
    const float faceScale = 0.32f * static_cast<float>(asset.scale);
    const QVector3D sourceHeadCenter(0.0f, 2.55f, 0.0f);
    transformed.reserve(model.vertices.size());
    for (const Vertex& source : model.vertices) {
        // The generated expression assets are busts. Keep only their neck/top
        // and head region when they are attached to a simple human figure.
        if (source.position.y() < 1.30f) continue;
        Vertex vertex = source;
        vertex.position = head + (source.position - sourceHeadCenter) * faceScale;
        transformed.push_back(vertex);
    }
    if (transformed.isEmpty()) return false;
    drawVertices(transformed, GL_TRIANGLES);
    return true;
}

void PaintingCanvas::drawImportedHuman(const PaintingAsset& asset) {
    if (!myImportedHuman.loaded || asset.joints.size() != humanJointCount()) return;

    QMatrix4x4 normalize;
    normalize.setToIdentity();
    normalize.translate(myImportedHuman.normalizationOffset);
    normalize.scale(myImportedHuman.normalizationScale);

    QVector<QVector3D> desired(myImportedHuman.joints.size());
    QVector<bool> hasDesired(myImportedHuman.joints.size(), false);
    for (int node = 0; node < myImportedHuman.joints.size(); ++node) {
        const int appJoint = myImportedHuman.joints.at(node).appJoint;
        if (appJoint >= 0 && appJoint < asset.joints.size()) {
            desired[node] = asset.joints.at(appJoint);
            hasDesired[node] = true;
        }
    }

    QVector<QMatrix4x4> poseGlobals;
    poseGlobals.reserve(myImportedHuman.joints.size());
    for (const ImportedJoint& joint : myImportedHuman.joints) poseGlobals.push_back(joint.restGlobal);

    const auto restPosition = [&](int node) {
        return myImportedHuman.joints.at(node).restGlobal.map(QVector3D(0, 0, 0));
    };
    const auto targetDirection = [&](int node, int& referenceNode) {
        referenceNode = -1;
        const int nodeAppJoint = myImportedHuman.joints.at(node).appJoint;
        if (nodeAppJoint >= 0) {
            // Prefer the corresponding child in the app's human hierarchy.
            // This matters at pelvis/chest, where the imported armature also
            // contains an unexposed spine bone between the two controls.
            for (int candidate = 0; candidate < myImportedHuman.joints.size(); ++candidate) {
                const int candidateAppJoint = myImportedHuman.joints.at(candidate).appJoint;
                if (candidateAppJoint >= 0 && hasDesired[candidate] &&
                    humanJointParent(candidateAppJoint) == nodeAppJoint) {
                    referenceNode = candidate;
                    return desired.at(candidate) - desired.at(node);
                }
            }
        }
        for (int candidate = 0; candidate < myImportedHuman.joints.size(); ++candidate) {
            if (myImportedHuman.joints.at(candidate).parent == node && myImportedHuman.joints.at(candidate).appJoint >= 0 && hasDesired[candidate]) {
                referenceNode = candidate;
                return desired.at(candidate) - desired.at(node);
            }
        }
        const int parent = myImportedHuman.joints.at(node).parent;
        if (parent >= 0 && hasDesired[parent]) {
            referenceNode = parent;
            return desired.at(node) - desired.at(parent);
        }
        return QVector3D();
    };

    for (int node : myImportedHuman.skinJoints) {
        if (node < 0 || node >= myImportedHuman.joints.size()) continue;
        if (hasDesired[node]) {
            int referenceNode = -1;
            const QVector3D wantedDirection = targetDirection(node, referenceNode);
            QVector3D restDirection;
            if (referenceNode >= 0) restDirection = restPosition(referenceNode) - restPosition(node);
            if (wantedDirection.lengthSquared() > 0.00001f && restDirection.lengthSquared() > 0.00001f) {
                const QQuaternion arc = QQuaternion::rotationTo(restDirection.normalized(), wantedDirection.normalized());
                // Preserve Blender's exported rest matrix exactly. Rebuilding
                // its quaternion from a normalized matrix loses the armature's
                // bone-axis basis and makes the untouched model explode.
                QMatrix4x4 poseDelta;
                poseDelta.setToIdentity();
                poseDelta.rotate(arc);
                QMatrix4x4 pose = poseDelta * myImportedHuman.joints.at(node).restGlobal;
                pose.setColumn(3, QVector4D(desired.at(node), 1.0f));
                poseGlobals[node] = pose;
            } else {
                QMatrix4x4 pose = myImportedHuman.joints.at(node).restGlobal;
                pose.setColumn(3, QVector4D(desired.at(node), 1.0f));
                poseGlobals[node] = pose;
            }
        }
    }

    // The imported joint matrices are model-local. Apply the canvas asset
    // transform to every bone, including helper bones such as root, spine,
    // and neutral_bone. Translating only the named controls separates mesh
    // regions that are weighted to those helper bones.
    QMatrix4x4 assetTransform;
    assetTransform.setToIdentity();
    assetTransform.translate(asset.position);
    assetTransform.scale(float(asset.scale));
    for (QMatrix4x4& pose : poseGlobals) pose = assetTransform * pose;

    QVector<QMatrix4x4> skinMatrices;
    skinMatrices.reserve(myImportedHuman.skinJoints.size());
    QMatrix4x4 inverseNormalization;
    inverseNormalization.setToIdentity();
    inverseNormalization.scale(QVector3D(
        1.0f / std::max(0.0001f, myImportedHuman.normalizationScale.x()),
        1.0f / std::max(0.0001f, myImportedHuman.normalizationScale.y()),
        1.0f / std::max(0.0001f, myImportedHuman.normalizationScale.z())));
    inverseNormalization.translate(-myImportedHuman.normalizationOffset);
    for (int skinIndex = 0; skinIndex < myImportedHuman.skinJoints.size(); ++skinIndex) {
        const int node = myImportedHuman.skinJoints.at(skinIndex);
        if (node < 0 || node >= poseGlobals.size()) {
            QMatrix4x4 identity;
            identity.setToIdentity();
            skinMatrices.push_back(identity);
            continue;
        }
        if (skinIndex < myImportedHuman.inverseBindMatrices.size()) {
            // glTF defines the joint transform as currentGlobal * inverseBind.
            // The vertices are normalized before reaching the shader, so move
            // the source-space inverse bind matrix into that normalized space.
            skinMatrices.push_back(poseGlobals.at(node) *
                                   myImportedHuman.inverseBindMatrices.at(skinIndex) *
                                   inverseNormalization);
        } else {
            skinMatrices.push_back(poseGlobals.at(node) * myImportedHuman.joints.at(node).restGlobal.inverted());
        }
    }

    QVector<Vertex> vertices;
    QVector<Vertex> staticVertices;
    for (const ImportedPrimitive& primitive : myImportedHuman.primitives) {
        QVector3D color(0.12f, 0.15f, 0.18f);
        if (primitive.name == QStringLiteral("base")) color = QVector3D(0.69f, 0.35f, 0.26f);
        else if (primitive.name.contains(QStringLiteral("long"), Qt::CaseInsensitive)) color = QVector3D(0.055f, 0.03f, 0.022f);
        else if (primitive.name.contains(QStringLiteral("suit"), Qt::CaseInsensitive)) color = QVector3D(0.16f, 0.20f, 0.23f);

        const auto appendVertex = [&](unsigned int vertexIndex) {
            if (vertexIndex >= unsigned(primitive.vertices.size())) return;
            const ImportedVertex& source = primitive.vertices.at(int(vertexIndex));
            const QVector3D normalizedPosition = normalize.map(primitive.modelMatrix.map(source.position));
            QVector3D normal = normalize.mapVector(primitive.modelMatrix.mapVector(source.normal)).normalized();
            if (primitive.skinned) vertices.push_back({normalizedPosition, color, normal, source.joints, source.weights});
            else staticVertices.push_back({normalizedPosition, color, normal});
        };
        for (int index = 0; index + 2 < primitive.indices.size(); index += 3) {
            if (primitive.name == QStringLiteral("base")) {
                const auto normalizedPoint = [&](int vertexIndex) {
                    return normalize.map(primitive.modelMatrix.map(primitive.vertices.at(int(vertexIndex)).position));
                };
                const QVector3D first = normalizedPoint(primitive.indices.at(index));
                const QVector3D second = normalizedPoint(primitive.indices.at(index + 1));
                const QVector3D third = normalizedPoint(primitive.indices.at(index + 2));
                const QVector3D center = (first + second + third) / 3.0f;
                // The source file contains a nude base mesh under the outfit.
                // Keep the skin for face, arms, and legs, but mask the exposed
                // groin region so the clothed study remains presentation-safe.
                if (std::abs(center.x()) < 0.34f && center.y() > 0.70f && center.y() < 1.62f && center.z() > 0.12f) continue;
            }
            appendVertex(primitive.indices.at(index));
            appendVertex(primitive.indices.at(index + 1));
            appendVertex(primitive.indices.at(index + 2));
        }
    }
    drawVertices(vertices, GL_TRIANGLES, &skinMatrices);
    drawVertices(staticVertices, GL_TRIANGLES);
}

void PaintingCanvas::drawHumanJointHandles(const PaintingAsset& asset) {
    if (asset.joints.size() != humanJointCount()) return;
    QVector<Vertex> handles;
    const QVector3D jointColor(0.95f, 0.68f, 0.20f);
    const QVector3D selectedColor(0.98f, 0.32f, 0.22f);
    for (int index = 0; index < asset.joints.size(); ++index) {
        if (index == static_cast<int>(HumanJoint::Neck)) continue;
        const QVector3D worldPoint = asset.position + asset.joints.at(index) * float(asset.scale);
        const QVector3D color = index == mySelectedJointIndex ? selectedColor : jointColor;
        const float radius = index == mySelectedJointIndex ? 0.20f : 0.17f;
        addSphere(handles, worldPoint, QVector3D(radius, radius, radius) * float(asset.scale), color);
    }
    glDisable(GL_DEPTH_TEST);
    drawVertices(handles, GL_TRIANGLES);
    glEnable(GL_DEPTH_TEST);
}

void PaintingCanvas::drawTree(const QVector3D& o, double s) {
    QVector<Vertex> vertices;
    addCylinder(vertices, o, o + QVector3D(0, 2.0f * s, 0), 0.22 * s, QVector3D(0.35f, 0.22f, 0.15f));
    addCone(vertices, o + QVector3D(0, 1.3f * s, 0), 1.2 * s, 2.5 * s, QVector3D(0.22f, 0.42f, 0.29f));
    addCone(vertices, o + QVector3D(0, 2.55f * s, 0), 0.92 * s, 1.9 * s, QVector3D(0.32f, 0.55f, 0.34f));
    drawVertices(vertices, GL_TRIANGLES);
}

void PaintingCanvas::drawMountain(const QVector3D& o, double s) {
    QVector<Vertex> vertices;
    addCone(vertices, o + QVector3D(0, 0, 0), 2.5 * s, 2.8 * s, QVector3D(0.30f, 0.39f, 0.45f));
    addCone(vertices, o + QVector3D(1.6f * s, 0, 0.5f * s), 1.7 * s, 2.0 * s, QVector3D(0.38f, 0.46f, 0.49f));
    drawVertices(vertices, GL_TRIANGLES);
}

void PaintingCanvas::drawRock(const QVector3D& o, double s) {
    QVector<Vertex> vertices;
    addSphere(vertices, o + QVector3D(0.0f, 0.5f * s, 0), QVector3D(1.2f, 0.55f, 0.85f) * float(s), QVector3D(0.43f, 0.45f, 0.42f));
    addSphere(vertices, o + QVector3D(0.9f * s, 0.4f * s, 0.25f * s), QVector3D(0.72f, 0.4f, 0.6f) * float(s), QVector3D(0.34f, 0.37f, 0.36f));
    drawVertices(vertices, GL_TRIANGLES);
}

void PaintingCanvas::drawLight(const QVector3D& o, double s) {
    QVector<Vertex> sphere;
    const QVector3D center = o + QVector3D(0, 1.5f * s, 0);
    addSphere(sphere, center, QVector3D(0.35f, 0.35f, 0.35f) * float(s), QVector3D(0.98f, 0.72f, 0.28f));
    drawVertices(sphere, GL_TRIANGLES);

    QVector<Vertex> rays;
    for (int i = 0; i < 8; ++i) {
        const float angle = 2.0f * kPi * float(i) / 8.0f;
        addLine(rays, center + QVector3D(std::cos(angle), 0, std::sin(angle)) * float(0.5 * s),
                center + QVector3D(std::cos(angle), 0, std::sin(angle)) * float(1.0 * s), QVector3D(0.95f, 0.67f, 0.22f));
    }
    glLineWidth(2.0f);
    drawVertices(rays, GL_LINES);
}

void PaintingCanvas::drawDowntownTower(const PaintingAsset& asset) {
    if (!myDowntownBuilding.loaded) {
        QVector<Vertex> fallback;
        addBox(fallback, asset.position + QVector3D(0.0f, 4.0f, 0.0f),
               QVector3D(3.0f, 8.0f, 3.0f), QVector3D(0.30f, 0.12f, 0.07f));
        drawVertices(fallback, GL_TRIANGLES);
        return;
    }

    QVector<Vertex> transformed;
    transformed.reserve(myDowntownBuilding.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : myDowntownBuilding.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawResidentialNeighborhood(const PaintingAsset& asset) {
    if (!myResidentialNeighborhood.loaded) {
        QVector<Vertex> fallback;
        addBox(fallback, asset.position + QVector3D(0.0f, 2.6f, 0.0f),
               QVector3D(5.5f, 5.2f, 3.5f), QVector3D(0.30f, 0.12f, 0.07f));
        drawVertices(fallback, GL_TRIANGLES);
        return;
    }

    QVector<Vertex> transformed;
    transformed.reserve(myResidentialNeighborhood.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : myResidentialNeighborhood.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawResidentialIntersection(const PaintingAsset& asset) {
    if (!myResidentialIntersection.loaded) {
        QVector<Vertex> fallback;
        addBox(fallback, asset.position + QVector3D(0.0f, 1.0f, 0.0f),
               QVector3D(8.0f, 2.0f, 8.0f), QVector3D(0.12f, 0.13f, 0.14f));
        drawVertices(fallback, GL_TRIANGLES);
        return;
    }

    QVector<Vertex> transformed;
    transformed.reserve(myResidentialIntersection.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : myResidentialIntersection.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawResidentialAvenue(const PaintingAsset& asset) {
    if (!myResidentialAvenue.loaded) {
        QVector<Vertex> fallback;
        addBox(fallback, asset.position + QVector3D(0.0f, 1.0f, 0.0f),
               QVector3D(12.0f, 2.0f, 3.0f), QVector3D(0.12f, 0.13f, 0.14f));
        drawVertices(fallback, GL_TRIANGLES);
        return;
    }

    QVector<Vertex> transformed;
    transformed.reserve(myResidentialAvenue.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : myResidentialAvenue.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawOcean(const PaintingAsset& asset) {
    if (!myOcean.loaded) return;

    QVector<Vertex> transformed;
    transformed.reserve(myOcean.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : myOcean.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawGrandHarbour(const PaintingAsset& asset) {
    if (!myGrandHarbour.loaded) return;

    QVector<Vertex> transformed;
    transformed.reserve(myGrandHarbour.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : myGrandHarbour.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawFacialExpression(const PaintingAsset& asset) {
    const QStringList names = facialExpressionNames();
    const int index = names.indexOf(asset.expression);
    if (index < 0 || index >= myFacialExpressions.size() || !myFacialExpressions.at(index).loaded) return;

    const DowntownBuilding& model = myFacialExpressions.at(index);
    QVector<Vertex> transformed;
    transformed.reserve(model.vertices.size());
    const float scale = static_cast<float>(asset.scale);
    for (const Vertex& source : model.vertices) {
        Vertex vertex = source;
        vertex.position = asset.position + source.position * scale;
        transformed.push_back(vertex);
    }
    const GLboolean blendingWasEnabled = glIsEnabled(GL_BLEND);
    glDisable(GL_BLEND);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    drawVertices(transformed, GL_TRIANGLES);
    if (blendingWasEnabled) glEnable(GL_BLEND);
}

void PaintingCanvas::drawBaseballBat(const PaintingAsset& asset) {
    const PaintingAsset* human = nullptr;
    for (const PaintingAsset& candidate : myAssets) {
        if (candidate.id == asset.attachedHumanId && candidate.type == AssetType::Human) {
            human = &candidate;
            break;
        }
    }
    const auto worldJoint = [&](HumanJoint joint) {
        if (!human || human->joints.size() != humanJointCount()) return asset.position;
        return human->position + human->joints.at(static_cast<int>(joint)) * float(human->scale);
    };
    const QVector3D leftHand = worldJoint(HumanJoint::WristLeft);
    const QVector3D rightHand = worldJoint(HumanJoint::WristRight);
    const QVector3D gripCenter = human ? (leftHand + rightHand) * 0.5f : asset.position;
    const QVector3D batBottom = gripCenter + QVector3D(0.0f, -0.35f, 0.0f);
    const QVector3D batHandleTop = batBottom + QVector3D(0.0f, 0.72f, 0.0f);
    const QVector3D batBarrelTop = batHandleTop + QVector3D(0.0f, 1.35f, 0.0f);
    const QVector3D wood(0.62f, 0.30f, 0.10f);
    const QVector3D handleColor(0.28f, 0.15f, 0.08f);
    const QVector3D handColor(0.38f, 0.53f, 0.62f);
    QVector<Vertex> vertices;
    addCylinder(vertices, batBottom, batHandleTop, 0.075, handleColor);
    addCylinder(vertices, batHandleTop, batBarrelTop, 0.17, wood);
    addSphere(vertices, batBottom, QVector3D(0.10f, 0.10f, 0.10f), handleColor);
    addSphere(vertices, batBarrelTop, QVector3D(0.18f, 0.18f, 0.18f), wood);

    const auto wrappedHand = [&](const QVector3D& hand) {
        addSphere(vertices, hand, QVector3D(0.19f, 0.15f, 0.18f), handColor);
        for (int finger = 0; finger < 4; ++finger) {
            const float offset = (float(finger) - 1.5f) * 0.065f;
            const float fingerY = hand.y() + (float(finger) - 1.5f) * 0.055f;
            const QVector3D front = QVector3D(hand.x() + offset, fingerY, hand.z() - 0.20f);
            const QVector3D around = QVector3D(hand.x() + offset, fingerY + 0.035f, hand.z() - 0.055f);
            const QVector3D back = QVector3D(hand.x() + offset * 0.82f, fingerY + 0.01f, hand.z() + 0.055f);
            addCylinder(vertices, front, around, 0.035, handColor);
            addCylinder(vertices, around, back, 0.035, handColor);
            addSphere(vertices, back, QVector3D(0.04f, 0.04f, 0.04f), handColor);
        }
    };
    if (human) {
        wrappedHand(leftHand);
        wrappedHand(rightHand);
    }
    drawVertices(vertices, GL_TRIANGLES);
}

void PaintingCanvas::drawPistol(const PaintingAsset& asset) {
    const PaintingAsset* human = nullptr;
    for (const PaintingAsset& candidate : myAssets) {
        if (candidate.id == asset.attachedHumanId && candidate.type == AssetType::Human) {
            human = &candidate;
            break;
        }
    }
    const QVector3D grip = human && human->joints.size() == humanJointCount()
        ? human->position + human->joints.at(static_cast<int>(HumanJoint::WristRight)) * float(human->scale)
        : asset.position;
    const QVector3D metal(0.12f, 0.14f, 0.16f);
    const QVector3D gripColor(0.055f, 0.065f, 0.075f);
    const QVector3D handColor(0.38f, 0.53f, 0.62f);
    QVector<Vertex> vertices;
    const QVector3D gripBottom = grip + QVector3D(0.0f, -0.38f, 0.0f);
    addCylinder(vertices, grip, gripBottom, 0.11, gripColor);
    // The pistol points forward along -Z, away from the body's chest, rather
    // than sideways across the human's shoulders.
    addBox(vertices, grip + QVector3D(0.0f, 0.04f, -0.42f), QVector3D(0.28f, 0.18f, 0.84f), metal);
    addCylinder(vertices, grip + QVector3D(0.0f, 0.04f, -0.82f), grip + QVector3D(0.0f, 0.04f, -1.18f), 0.065, metal);
    addSphere(vertices, grip + QVector3D(0.0f, 0.04f, -1.20f), QVector3D(0.075f, 0.075f, 0.075f), metal);
    addSphere(vertices, grip, QVector3D(0.19f, 0.15f, 0.18f), handColor);
    for (int finger = 0; finger < 4; ++finger) {
        const float offset = (float(finger) - 1.5f) * 0.065f;
        const float fingerY = grip.y() + (float(finger) - 1.5f) * 0.055f;
        const QVector3D front = QVector3D(grip.x() + offset, fingerY, grip.z() - 0.20f);
        const QVector3D around = QVector3D(grip.x() + offset, fingerY + 0.035f, grip.z() - 0.055f);
        const QVector3D back = QVector3D(grip.x() + offset * 0.82f, fingerY + 0.01f, grip.z() + 0.055f);
        addCylinder(vertices, front, around, 0.035, handColor);
        addCylinder(vertices, around, back, 0.035, handColor);
        addSphere(vertices, back, QVector3D(0.04f, 0.04f, 0.04f), handColor);
    }
    drawVertices(vertices, GL_TRIANGLES);
}

#if 0
void PaintingCanvas::drawTextBlock(const PaintingAsset& asset) {
    const QVector3D origin = asset.position;
    const QVector3D dimensions = asset.textBlockDimensions;
    const float length = dimensions.x();
    const float breadth = dimensions.y();
    const float width = dimensions.z();
    QVector<Vertex> vertices;
    addBox(vertices, QVector3D(0.0f, breadth * 0.5f, 0.0f), dimensions,
           QVector3D(0.19f, 0.28f, 0.34f));

    const QString label = asset.text.isEmpty() ? QStringLiteral("TEXT") : asset.text.left(8).toUpper();
    const float glyphWidth = 0.60f;
    const float cellX = 0.12f;
    const float cellY = 0.16f;
    const float spacing = 0.14f;
    const float availableLength = std::max(0.2f, length - 0.5f);
    const float availableBreadth = std::max(0.2f, breadth - 0.4f);
    const float naturalLabelWidth = label.size() * glyphWidth + (label.size() - 1) * spacing;
    const float naturalLabelHeight = 7.0f * cellY;
    // Keep the lettering proportional to the block. Larger dimensions should
    // enlarge the raised text too, rather than leaving it at its default size.
    const float glyphScaleX = availableLength / std::max(0.01f, naturalLabelWidth);
    const float glyphScaleY = availableBreadth / std::max(0.01f, naturalLabelHeight);
    const float scaledCellX = cellX * glyphScaleX;
    const float scaledCellY = cellY * glyphScaleY;
    const float scaledSpacing = spacing * glyphScaleX;
    const float scaledGlyphWidth = glyphWidth * glyphScaleX;
    const float labelWidth = label.size() * scaledGlyphWidth + (label.size() - 1) * scaledSpacing;
    const float startX = -labelWidth * 0.5f;
    const float labelHeight = 7.0f * scaledCellY;
    const float labelBottom = (breadth - labelHeight) * 0.5f;
    const float letterDepth = width * 0.35f;
    const QVector3D letterColor(0.91f, 0.76f, 0.43f);

    for (int characterIndex = 0; characterIndex < label.size(); ++characterIndex) {
        QVector<QString> pattern;
        switch (label.at(characterIndex).toLatin1()) {
        case 'A': pattern = {"01110", "10001", "10001", "11111", "10001", "10001", "10001"}; break;
        case 'B': pattern = {"11110", "10001", "11110", "10001", "10001", "10001", "11110"}; break;
        case 'C': pattern = {"01111", "10000", "10000", "10000", "10000", "10000", "01111"}; break;
        case 'D': pattern = {"11110", "10001", "10001", "10001", "10001", "10001", "11110"}; break;
        case 'E': pattern = {"11111", "10000", "10000", "11110", "10000", "10000", "11111"}; break;
        case 'F': pattern = {"11111", "10000", "10000", "11110", "10000", "10000", "10000"}; break;
        case 'G': pattern = {"01111", "10000", "10000", "10111", "10001", "10001", "01111"}; break;
        case 'H': pattern = {"10001", "10001", "10001", "11111", "10001", "10001", "10001"}; break;
        case 'I': pattern = {"11111", "00100", "00100", "00100", "00100", "00100", "11111"}; break;
        case 'J': pattern = {"00111", "00010", "00010", "00010", "10010", "10010", "01100"}; break;
        case 'K': pattern = {"10001", "10010", "10100", "11000", "10100", "10010", "10001"}; break;
        case 'L': pattern = {"10000", "10000", "10000", "10000", "10000", "10000", "11111"}; break;
        case 'M': pattern = {"10001", "11011", "10101", "10101", "10001", "10001", "10001"}; break;
        case 'N': pattern = {"10001", "11001", "10101", "10011", "10001", "10001", "10001"}; break;
        case 'O': pattern = {"01110", "10001", "10001", "10001", "10001", "10001", "01110"}; break;
        case 'P': pattern = {"11110", "10001", "10001", "11110", "10000", "10000", "10000"}; break;
        case 'Q': pattern = {"01110", "10001", "10001", "10001", "10101", "10010", "01101"}; break;
        case 'R': pattern = {"11110", "10001", "10001", "11110", "10100", "10010", "10001"}; break;
        case 'S': pattern = {"01111", "10000", "10000", "01110", "00001", "00001", "11110"}; break;
        case 'T': pattern = {"11111", "00100", "00100", "00100", "00100", "00100", "00100"}; break;
        case 'U': pattern = {"10001", "10001", "10001", "10001", "10001", "10001", "01110"}; break;
        case 'V': pattern = {"10001", "10001", "10001", "10001", "10001", "01010", "00100"}; break;
        case 'W': pattern = {"10001", "10001", "10001", "10101", "10101", "11011", "10001"}; break;
        case 'X': pattern = {"10001", "10001", "01010", "00100", "01010", "10001", "10001"}; break;
        case 'Y': pattern = {"10001", "10001", "01010", "00100", "00100", "00100", "00100"}; break;
        case 'Z': pattern = {"11111", "00001", "00010", "00100", "01000", "10000", "11111"}; break;
        case '0': pattern = {"01110", "10001", "10011", "10101", "11001", "10001", "01110"}; break;
        case '1': pattern = {"00100", "01100", "00100", "00100", "00100", "00100", "01110"}; break;
        case '2': pattern = {"01110", "10001", "00001", "00010", "00100", "01000", "11111"}; break;
        case '3': pattern = {"11110", "00001", "00001", "01110", "00001", "00001", "11110"}; break;
        case '4': pattern = {"00010", "00110", "01010", "10010", "11111", "00010", "00010"}; break;
        case '5': pattern = {"11111", "10000", "10000", "11110", "00001", "00001", "11110"}; break;
        case '6': pattern = {"01110", "10000", "10000", "11110", "10001", "10001", "01110"}; break;
        case '7': pattern = {"11111", "00001", "00010", "00100", "01000", "01000", "01000"}; break;
        case '8': pattern = {"01110", "10001", "10001", "01110", "10001", "10001", "01110"}; break;
        case '9': pattern = {"01110", "10001", "10001", "01111", "00001", "00001", "01110"}; break;
        default: pattern = {"00000", "00000", "00000", "00000", "00000", "00000", "00000"}; break;
        }

        const float glyphLeft = startX + characterIndex * (scaledGlyphWidth + scaledSpacing);
        for (int row = 0; row < pattern.size(); ++row) {
            for (int column = 0; column < pattern.at(row).size(); ++column) {
                if (pattern.at(row).at(column) != QLatin1Char('1')) continue;
                const float x = glyphLeft + (column + 0.5f) * scaledCellX;
                const float y = labelBottom + (6 - row + 0.5f) * scaledCellY;
                addBox(vertices, origin + QVector3D(x, y, width * 0.5f + letterDepth * 0.5f),
                       QVector3D(scaledCellX * 0.95f, scaledCellY * 0.95f, letterDepth), letterColor);
            }
        }
    }
    drawVertices(vertices, GL_TRIANGLES);
}

#endif

void PaintingCanvas::drawTextBlock(const PaintingAsset& asset) {
    const QVector3D origin = asset.position;
    const QVector3D dimensions = asset.textBlockDimensions;
    const float length = dimensions.x();
    const float breadth = dimensions.y();
    const float width = dimensions.z();
    QVector<Vertex> vertices;
    if (!myRenderMode) {
        addBox(vertices, origin + QVector3D(0.0f, breadth * 0.5f, 0.0f), dimensions,
               QVector3D(0.19f, 0.28f, 0.34f));
    }

    const QString label = asset.text.isEmpty() ? QStringLiteral("TEXT") : asset.text.left(8);
    QFont font(asset.fontFamily);
    font.setPointSize(100);
    font.setItalic(asset.fontItalic);
    font.setBold(asset.fontBold);
    QPainterPath path;
    if (asset.textVertical) {
        const QFontMetricsF metrics(font);
        const qreal lineHeight = metrics.lineSpacing();
        for (int index = 0; index < label.size(); ++index) {
            path.addText(0.0, lineHeight * index, font, label.mid(index, 1));
        }
    } else {
        path.addText(0.0, 0.0, font, label);
    }
    const QRectF bounds = path.boundingRect();
    if (bounds.isEmpty()) {
        drawVertices(vertices, GL_TRIANGLES);
        return;
    }

    const float targetLength = std::max(0.2f, length - 0.45f);
    const float targetBreadth = std::max(0.2f, breadth - 0.35f);
    const float glyphScale = std::min(targetLength / static_cast<float>(bounds.width()),
                                      targetBreadth / static_cast<float>(bounds.height()));
    const QPointF center = bounds.center();
    const float textDepth = width * 0.35f;
    const float frontZ = width * 0.5f + textDepth;
    const float sideZ = width * 0.5f;
    const QVector3D frontColor(0.91f, 0.76f, 0.43f);
    const QVector3D sideColor(0.60f, 0.43f, 0.22f);

    const auto mapPoint = [&](const QPointF& point) {
        return QPointF((point.x() - center.x()) * glyphScale,
                       (center.y() - point.y()) * glyphScale + breadth * 0.5);
    };
    const auto cross = [](const QPointF& a, const QPointF& b, const QPointF& c) {
        return (b.x() - a.x()) * (c.y() - a.y()) - (b.y() - a.y()) * (c.x() - a.x());
    };
    const auto contains = [&](const QPointF& point, const QPointF& a, const QPointF& b, const QPointF& c) {
        const double first = cross(point, a, b);
        const double second = cross(point, b, c);
        const double third = cross(point, c, a);
        const bool hasNegative = first < -0.00001 || second < -0.00001 || third < -0.00001;
        const bool hasPositive = first > 0.00001 || second > 0.00001 || third > 0.00001;
        return !(hasNegative && hasPositive);
    };

    const auto triangulate = [&](QVector<QPointF> polygon) {
        QVector<QVector<QPointF>> triangles;
        if (polygon.size() > 1 && polygon.first() == polygon.last()) polygon.removeLast();
        if (polygon.size() < 3) return triangles;

        double area = 0.0;
        for (int index = 0; index < polygon.size(); ++index) {
            const QPointF& current = polygon.at(index);
            const QPointF& next = polygon.at((index + 1) % polygon.size());
            area += current.x() * next.y() - next.x() * current.y();
        }
        if (area < 0.0) std::reverse(polygon.begin(), polygon.end());

        QVector<int> remaining;
        for (int index = 0; index < polygon.size(); ++index) remaining.push_back(index);
        int guard = 0;
        while (remaining.size() > 3 && guard++ < polygon.size() * polygon.size()) {
            bool clipped = false;
            for (int index = 0; index < remaining.size(); ++index) {
                const int previousIndex = remaining.at((index + remaining.size() - 1) % remaining.size());
                const int currentIndex = remaining.at(index);
                const int nextIndex = remaining.at((index + 1) % remaining.size());
                const QPointF& previous = polygon.at(previousIndex);
                const QPointF& current = polygon.at(currentIndex);
                const QPointF& next = polygon.at(nextIndex);
                if (cross(previous, current, next) <= 0.00001) continue;

                bool containsOther = false;
                for (int candidate : remaining) {
                    if (candidate == previousIndex || candidate == currentIndex || candidate == nextIndex) continue;
                    if (contains(polygon.at(candidate), previous, current, next)) {
                        containsOther = true;
                        break;
                    }
                }
                if (containsOther) continue;
                triangles.push_back({previous, current, next});
                remaining.removeAt(index);
                clipped = true;
                break;
            }
            if (!clipped) return QVector<QVector<QPointF>>();
        }
        if (remaining.size() == 3) {
            triangles.push_back({polygon.at(remaining.at(0)), polygon.at(remaining.at(1)), polygon.at(remaining.at(2))});
        }
        return triangles;
    };
    const auto addTriangle = [&](const QVector<QPointF>& triangle, float z, const QVector3D& color) {
        for (const QPointF& point : triangle) {
            vertices.push_back({QVector3D(point.x(), point.y(), z), color});
        }
    };

    // Qt converts the selected font's outlines into fill polygons. These are
    // triangulated for the front face, then their contours are extruded.
    const QList<QPolygonF> fillPolygons = path.toFillPolygons();
    for (const QPolygonF& sourcePolygon : fillPolygons) {
        QVector<QPointF> polygon;
        for (const QPointF& point : sourcePolygon) polygon.push_back(mapPoint(point));
        const QVector<QVector<QPointF>> triangles = triangulate(polygon);
        for (const QVector<QPointF>& triangle : triangles) addTriangle(triangle, frontZ, frontColor);
    }

    const QList<QPolygonF> sidePolygons = path.toSubpathPolygons();
    for (const QPolygonF& sourcePolygon : sidePolygons) {
        QVector<QPointF> polygon;
        for (const QPointF& point : sourcePolygon) polygon.push_back(mapPoint(point));
        if (polygon.size() > 1 && polygon.first() == polygon.last()) polygon.removeLast();
        for (int index = 0; index < polygon.size(); ++index) {
            const QPointF& first = polygon.at(index);
            const QPointF& second = polygon.at((index + 1) % polygon.size());
            vertices.push_back({QVector3D(first.x(), first.y(), sideZ), sideColor});
            vertices.push_back({QVector3D(second.x(), second.y(), sideZ), sideColor});
            vertices.push_back({QVector3D(second.x(), second.y(), frontZ), sideColor});
            vertices.push_back({QVector3D(first.x(), first.y(), sideZ), sideColor});
            vertices.push_back({QVector3D(second.x(), second.y(), frontZ), sideColor});
            vertices.push_back({QVector3D(first.x(), first.y(), frontZ), sideColor});
        }
    }
    const auto toWorld = [&](const QVector3D& local) {
        if (!asset.textFlat) return origin + local;
        // Rotate onto the ground while preserving the text's reading
        // direction when viewed from above.
        return origin + QVector3D(local.x(), local.z() + width * 0.5f, breadth * 0.5f - local.y());
    };
    for (Vertex& vertex : vertices) vertex.position = toWorld(vertex.position);
    drawVertices(vertices, GL_TRIANGLES);
}

void PaintingCanvas::drawSelection(const PaintingAsset& asset) {
    const QVector3D min = assetBoundsMin(asset) - QVector3D(0.08f, -0.02f, 0.08f);
    const QVector3D max = assetBoundsMax(asset) + QVector3D(0.08f, 0.08f, 0.08f);
    QVector<Vertex> vertices;
    const QVector3D color(0.92f, 0.63f, 0.20f);
    const QVector3D p[8] = {QVector3D(min.x(), min.y(), min.z()), QVector3D(max.x(), min.y(), min.z()),
                            QVector3D(max.x(), max.y(), min.z()), QVector3D(min.x(), max.y(), min.z()),
                            QVector3D(min.x(), min.y(), max.z()), QVector3D(max.x(), min.y(), max.z()),
                            QVector3D(max.x(), max.y(), max.z()), QVector3D(min.x(), max.y(), max.z())};
    const int edges[24] = {0,1, 1,2, 2,3, 3,0, 4,5, 5,6, 6,7, 7,4, 0,4, 1,5, 2,6, 3,7};
    for (int edge : edges) vertices.push_back({p[edge], color});
    glLineWidth(2.0f);
    drawVertices(vertices, GL_LINES);
}
