module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam BEATS = LEN / LANES;
    localparam CNT_W = $clog2(BEATS + 1);

    reg [CNT_W-1:0] cnt;
    reg signed [ACC_W-1:0] acc;
    reg result_valid_reg;

    wire done = (cnt == BEATS[CNT_W-1:0]);
    wire accepting = result_valid_reg ? (out_ready && out_valid) : 1'b1;
    wire beat_valid = accepting & in_a_flat_valid & in_b_flat_valid & ~done;

    assign in_a_flat_ready = beat_valid;
    assign in_b_flat_ready = beat_valid;
    assign out_valid = result_valid_reg;
    assign out_c = acc;

    reg signed [ACC_W-1:0] mac_sum;
    integer k;

    always @(*) begin
        mac_sum = 0;
        for (k = 0; k < LANES; k = k + 1)
            mac_sum = mac_sum + $signed(in_a_flat[k*DATA_W +: DATA_W]) * $signed(in_b_flat[k*DATA_W +: DATA_W]);
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt <= 0;
            acc <= 0;
            result_valid_reg <= 0;
        end else if (accepting && result_valid_reg) begin
            acc <= 0;
            cnt <= 0;
            result_valid_reg <= 0;
        end else if (beat_valid) begin
            cnt <= cnt + 1;
            acc <= acc + mac_sum;
            if (cnt == BEATS[CNT_W-1:0] - 1)
                result_valid_reg <= 1;
        end
    end

endmodule
