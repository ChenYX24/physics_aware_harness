#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "ADPPhysicsRuntimeLibrary.generated.h"

class AADPPhysicsRuntimeDriver;
class AActor;
class UPhysicsConstraintComponent;

UCLASS()
class ADPPHYSICSRUNTIME_API UADPPhysicsRuntimeLibrary : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	UFUNCTION(BlueprintCallable, Category = "ADP Physics", meta = (WorldContext = "WorldContextObject"))
	static AADPPhysicsRuntimeDriver* SpawnPhysicsRuntimeDriver(UObject* WorldContextObject);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Constraints")
	static bool InitializePhysicsConstraint(UPhysicsConstraintComponent* ConstraintComponent);

#if WITH_EDITOR
	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Geometry Collection")
	static FString CaptureGeometryCollectionFragmentState(AActor* GeometryCollectionActor);

	UFUNCTION(BlueprintCallable, Category = "ADP Physics|Assets")
	static bool CreateFracturedGlassPanelAsset(
		const FString& SourceMeshPath,
		const FString& MaterialPath,
		const FString& OutputPackagePath,
		FVector PanelSizeCm,
		FVector FractureCenterLocalCm,
		int32 VoronoiSites,
		int32 RandomSeed);
#endif
};
