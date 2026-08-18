#include "ADPPhysicsRuntimeLibrary.h"

#include "ADPPhysicsRuntimeDriver.h"
#include "Engine/Engine.h"
#include "Engine/World.h"

#if WITH_EDITOR
#include "AssetRegistry/AssetRegistryModule.h"
#include "Dom/JsonObject.h"
#include "Dataflow/DataflowSelection.h"
#include "Engine/StaticMesh.h"
#include "FractureEngineFracturing.h"
#include "GeometryCollection/GeometryCollectionAlgo.h"
#include "GeometryCollection/GeometryCollectionClusteringUtility.h"
#include "GeometryCollection/GeometryCollectionComponent.h"
#include "GeometryCollection/GeometryCollectionEngineConversion.h"
#include "GeometryCollection/GeometryCollectionObject.h"
#include "GameFramework/Actor.h"
#include "Materials/MaterialInterface.h"
#include "Misc/PackageName.h"
#include "Serialization/JsonSerializer.h"
#include "UObject/Package.h"
#include "UObject/SavePackage.h"
#endif

AADPPhysicsRuntimeDriver* UADPPhysicsRuntimeLibrary::SpawnPhysicsRuntimeDriver(UObject* WorldContextObject)
{
	UWorld* World = nullptr;
	if (GEngine != nullptr && WorldContextObject != nullptr)
	{
		World = GEngine->GetWorldFromContextObject(WorldContextObject, EGetWorldErrorMode::ReturnNull);
	}
	if (World == nullptr && WorldContextObject != nullptr)
	{
		World = WorldContextObject->GetWorld();
	}
	if (World == nullptr)
	{
		return nullptr;
	}

	FActorSpawnParameters Params;
	Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	AADPPhysicsRuntimeDriver* Driver = World->SpawnActor<AADPPhysicsRuntimeDriver>(
		AADPPhysicsRuntimeDriver::StaticClass(),
		FVector::ZeroVector,
		FRotator::ZeroRotator,
		Params);
#if WITH_EDITOR
	if (Driver != nullptr)
	{
		Driver->SetActorLabel(TEXT("ADPPhysicsRuntimeDriver"));
	}
#endif
	return Driver;
}

#if WITH_EDITOR
FString UADPPhysicsRuntimeLibrary::CaptureGeometryCollectionFragmentState(AActor* GeometryCollectionActor)
{
	UGeometryCollectionComponent* Component = GeometryCollectionActor != nullptr
		? GeometryCollectionActor->FindComponentByClass<UGeometryCollectionComponent>()
		: nullptr;
	const UGeometryCollection* RestCollection = Component != nullptr ? Component->GetRestCollection() : nullptr;
	const FGeometryCollection* Geometry = RestCollection != nullptr ? RestCollection->GetGeometryCollection().Get() : nullptr;
	if (Component == nullptr || Geometry == nullptr)
	{
		return TEXT("");
	}

	const TArray<FTransform3f>& ComponentSpaceTransforms = Component->GetComponentSpaceTransforms3f();
	const int32 TransformCount = FMath::Min(ComponentSpaceTransforms.Num(), Geometry->Transform.Num());
	TArray<TSharedPtr<FJsonValue>> Fragments;
	const FTransform ComponentToWorld = Component->GetComponentTransform();
	auto Quantize = [](double Value, double Step)
	{
		return FMath::RoundToDouble(Value / Step) * Step;
	};
	for (int32 TransformIndex = 0; TransformIndex < TransformCount; ++TransformIndex)
	{
		if (Geometry->Children[TransformIndex].Num() > 0)
		{
			continue;
		}
		const FTransform WorldTransform = FTransform(ComponentSpaceTransforms[TransformIndex]) * ComponentToWorld;
		const FVector Location = WorldTransform.GetLocation();
		FQuat Rotation = WorldTransform.GetRotation().GetNormalized();
		if (Rotation.W < 0.0)
		{
			Rotation.X *= -1.0;
			Rotation.Y *= -1.0;
			Rotation.Z *= -1.0;
			Rotation.W *= -1.0;
		}

		TSharedPtr<FJsonObject> Fragment = MakeShared<FJsonObject>();
		Fragment->SetNumberField(TEXT("index"), TransformIndex);
		Fragment->SetArrayField(TEXT("location_cm"), {
			MakeShared<FJsonValueNumber>(Quantize(Location.X, 0.01)),
			MakeShared<FJsonValueNumber>(Quantize(Location.Y, 0.01)),
			MakeShared<FJsonValueNumber>(Quantize(Location.Z, 0.01)),
		});
		Fragment->SetArrayField(TEXT("rotation_xyzw"), {
			MakeShared<FJsonValueNumber>(Quantize(Rotation.X, 0.00001)),
			MakeShared<FJsonValueNumber>(Quantize(Rotation.Y, 0.00001)),
			MakeShared<FJsonValueNumber>(Quantize(Rotation.Z, 0.00001)),
			MakeShared<FJsonValueNumber>(Quantize(Rotation.W, 0.00001)),
		});
		Fragments.Add(MakeShared<FJsonValueObject>(Fragment));
	}

	TSharedPtr<FJsonObject> Payload = MakeShared<FJsonObject>();
	Payload->SetStringField(TEXT("schema_version"), TEXT("geometry_collection_fragment_state_v1"));
	Payload->SetStringField(TEXT("space"), TEXT("world_cm"));
	Payload->SetNumberField(TEXT("location_quantization_cm"), 0.01);
	Payload->SetNumberField(TEXT("rotation_quantization"), 0.00001);
	TArray<TSharedPtr<FJsonValue>> DamageThresholds;
	for (const float Threshold : RestCollection->DamageThreshold)
	{
		DamageThresholds.Add(MakeShared<FJsonValueNumber>(Threshold));
	}
	Payload->SetArrayField(TEXT("damage_thresholds_runtime"), DamageThresholds);
	Payload->SetStringField(TEXT("damage_threshold_source"), TEXT("UGeometryCollection.DamageThreshold"));
	Payload->SetNumberField(TEXT("fragment_count"), Fragments.Num());
	Payload->SetArrayField(TEXT("fragments"), Fragments);
	FString Result;
	const TSharedRef<TJsonWriter<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>> Writer =
		TJsonWriterFactory<TCHAR, TCondensedJsonPrintPolicy<TCHAR>>::Create(&Result);
	FJsonSerializer::Serialize(Payload.ToSharedRef(), Writer);
	return Result;
}

bool UADPPhysicsRuntimeLibrary::CreateFracturedGlassPanelAsset(
	const FString& SourceMeshPath,
	const FString& MaterialPath,
	const FString& OutputPackagePath,
	FVector PanelSizeCm,
	FVector FractureCenterLocalCm,
	int32 VoronoiSites,
	int32 RandomSeed)
{
	UStaticMesh* SourceMesh = LoadObject<UStaticMesh>(nullptr, *SourceMeshPath);
	UMaterialInterface* Material = LoadObject<UMaterialInterface>(nullptr, *MaterialPath);
	if (SourceMesh == nullptr || Material == nullptr || !FPackageName::IsValidLongPackageName(OutputPackagePath))
	{
		UE_LOG(LogTemp, Error, TEXT("Invalid glass asset input: mesh=%s material=%s output=%s"), *SourceMeshPath, *MaterialPath, *OutputPackagePath);
		return false;
	}

	const FString AssetName = FPackageName::GetLongPackageAssetName(OutputPackagePath);
	const FString ObjectPath = OutputPackagePath + TEXT(".") + AssetName;
	UGeometryCollection* Collection = LoadObject<UGeometryCollection>(nullptr, *ObjectPath);
	const bool bNewAsset = Collection == nullptr;
	UPackage* Package = bNewAsset ? CreatePackage(*OutputPackagePath) : Collection->GetPackage();
	if (bNewAsset)
	{
		Collection = NewObject<UGeometryCollection>(
			Package,
			UGeometryCollection::StaticClass(),
			FName(*AssetName),
			RF_Transactional | RF_Public | RF_Standalone);
	}
	else
	{
		Collection->Modify();
		Collection->Reset();
		Collection->Materials.Reset();
		Collection->GeometrySource.Reset();
	}

	PanelSizeCm.X = FMath::Max(1.0, PanelSizeCm.X);
	PanelSizeCm.Y = FMath::Max(1.0, PanelSizeCm.Y);
	PanelSizeCm.Z = FMath::Max(1.0, PanelSizeCm.Z);
	const FTransform SourceTransform(FQuat::Identity, FVector::ZeroVector, PanelSizeCm / 100.0);
	TArray<UMaterialInterface*> Materials{Material};
	if (!FGeometryCollectionEngineConversion::AppendStaticMesh(
		SourceMesh,
		Materials,
		SourceTransform,
		Collection,
		true,
		true,
		false,
		false))
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to append watertight glass source mesh: %s"), *SourceMeshPath);
		return false;
	}

	decltype(FGeometryCollectionSource::SourceMaterial) SourceMaterials;
	SourceMaterials.Add(Material);
	Collection->GeometrySource.Emplace(FSoftObjectPath(SourceMesh), SourceTransform, SourceMaterials, false, false);
	Collection->InitializeMaterials(true);

	FManagedArrayCollection& ManagedCollection = *Collection->GetGeometryCollection();
	FDataflowTransformSelection Selection;
	Selection.InitializeFromCollection(ManagedCollection, true);
	FractureCenterLocalCm.X = FMath::Clamp(FractureCenterLocalCm.X, -PanelSizeCm.X * 0.45, PanelSizeCm.X * 0.45);
	FractureCenterLocalCm.Y = FMath::Clamp(FractureCenterLocalCm.Y, -PanelSizeCm.Y * 0.45, PanelSizeCm.Y * 0.45);
	FractureCenterLocalCm.Z = FMath::Clamp(FractureCenterLocalCm.Z, -PanelSizeCm.Z * 0.45, PanelSizeCm.Z * 0.45);
	const int32 TargetSites = FMath::Max(12, VoronoiSites);
	const int32 AngularSteps = FMath::Clamp(FMath::RoundToInt(FMath::Sqrt(static_cast<float>(TargetSites)) * 1.75f), 6, 16);
	const int32 RadialSteps = FMath::CeilToInt(static_cast<float>(TargetSites) / AngularSteps);
	const float RadiusCm = FMath::Min(PanelSizeCm.X, PanelSizeCm.Z) * 0.5f;
	FRandomStream Rand(RandomSeed);
	TArray<FVector> RadialSites;
	RadialSites.Reserve(TargetSites);
	for (int32 RingIndex = 0; RingIndex < RadialSteps && RadialSites.Num() < TargetSites; ++RingIndex)
	{
		const float RingFraction = (static_cast<float>(RingIndex) + 0.45f) / static_cast<float>(RadialSteps);
		const float RingRadius = RadiusCm * FMath::Pow(RingFraction, 1.15f);
		const float RingOffset = RingIndex % 2 == 0 ? 0.0f : PI / static_cast<float>(AngularSteps);
		for (int32 AngularIndex = 0; AngularIndex < AngularSteps && RadialSites.Num() < TargetSites; ++AngularIndex)
		{
			const float BaseAngle = 2.0f * PI * static_cast<float>(AngularIndex) / static_cast<float>(AngularSteps);
			const float Angle = BaseAngle + RingOffset + FMath::DegreesToRadians(Rand.FRandRange(-4.0f, 4.0f));
			const float NoisyRadius = RingRadius * Rand.FRandRange(0.94f, 1.06f);
			RadialSites.Add(FractureCenterLocalCm + FVector(
				NoisyRadius * FMath::Cos(Angle),
				Rand.FRandRange(-PanelSizeCm.Y * 0.1f, PanelSizeCm.Y * 0.1f),
				NoisyRadius * FMath::Sin(Angle)));
		}
	}
	if (FFractureEngineFracturing::VoronoiFracture(
		ManagedCollection,
		Selection,
		RadialSites,
		FTransform::Identity,
		RandomSeed,
		1.0f,
		true,
		0.0f,
		0.0f,
		0.1f,
		0.5f,
		2.0f,
		1,
		10.0f,
		true,
		2.0f) == INDEX_NONE)
	{
		UE_LOG(LogTemp, Error, TEXT("Failed to radial-fracture glass collection: %s"), *OutputPackagePath);
		return false;
	}

	FGeometryCollection* Geometry = Collection->GetGeometryCollection().Get();
	if (FGeometryCollectionClusteringUtility::ContainsMultipleRootBones(Geometry))
	{
		FGeometryCollectionClusteringUtility::ClusterAllBonesUnderNewRoot(Geometry, FName(TEXT("GlassPanelRoot")));
	}
	TArray<int32> RootBones;
	FGeometryCollectionClusteringUtility::GetRootBones(Geometry, RootBones);
	if (RootBones.Num() == 1 && Geometry->Children[RootBones[0]].Num() > 0)
	{
		Geometry->SimulationType[RootBones[0]] = FGeometryCollection::ESimulationTypes::FST_Clustered;
	}
	FGeometryCollectionClusteringUtility::UpdateHierarchyLevelOfChildren(Geometry, -1);
	UE_LOG(
		LogTemp,
		Display,
		TEXT("Generated glass hierarchy: transforms=%d roots=%d root=%d children=%d type=%d"),
		Geometry->Transform.Num(),
		RootBones.Num(),
		RootBones.Num() == 1 ? RootBones[0] : INDEX_NONE,
		RootBones.Num() == 1 ? Geometry->Children[RootBones[0]].Num() : 0,
		RootBones.Num() == 1 ? Geometry->SimulationType[RootBones[0]] : INDEX_NONE);
	GeometryCollectionAlgo::PrepareForSimulation(Geometry);
	FGeometryCollectionSizeSpecificData& CollisionData = Collection->GetDefaultSizeSpecificData();
	if (CollisionData.CollisionShapes.Num() == 0)
	{
		CollisionData.CollisionShapes.AddDefaulted();
	}
	FGeometryCollectionCollisionTypeData& CollisionShape = CollisionData.CollisionShapes[0];
	CollisionShape.CollisionType = ECollisionTypeEnum::Chaos_Surface_Volumetric;
	CollisionShape.ImplicitType = EImplicitTypeEnum::Chaos_Implicit_LevelSet;
	CollisionShape.LevelSet.MinLevelSetResolution = 12;
	CollisionShape.LevelSet.MaxLevelSetResolution = 24;
	CollisionShape.LevelSet.MinClusterLevelSetResolution = 16;
	CollisionShape.LevelSet.MaxClusterLevelSetResolution = 32;
	Collection->EnableClustering = true;
	Collection->DamageThreshold = {500000.0f, 50000.0f, 5000.0f};
	Collection->bMassAsDensity = true;
	Collection->Mass = 2500.0f;
	Collection->bRemoveOnMaxSleep = false;
	Collection->InvalidateCollection();
	Collection->RebuildRenderData();
	Collection->CreateSimulationData();
	Collection->MarkPackageDirty();
	Package->SetDirtyFlag(true);
	if (bNewAsset)
	{
		FAssetRegistryModule::AssetCreated(Collection);
	}

	FSavePackageArgs SaveArgs;
	SaveArgs.TopLevelFlags = RF_Public | RF_Standalone;
	SaveArgs.SaveFlags = SAVE_NoError;
	const FString Filename = FPackageName::LongPackageNameToFilename(OutputPackagePath, FPackageName::GetAssetPackageExtension());
	return UPackage::SavePackage(Package, Collection, *Filename, SaveArgs);
}
#endif
