using UnrealBuildTool;

public class ADPPhysicsRuntime : ModuleRules
{
	public ADPPhysicsRuntime(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"EnhancedInput",
			"GeometryCollectionEngine",
			"Json",
			"JsonUtilities",
			"PhysicsCore"
		});

		if (Target.bBuildEditor)
		{
			PrivateDependencyModuleNames.AddRange(new[]
			{
				"AssetRegistry",
				"Chaos",
				"DataflowCore",
				"FractureEngine"
			});
		}
	}
}
